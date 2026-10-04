#
# Gramps Web API - A RESTful API for the Gramps genealogy program
#
# Copyright (C) 2026      Tree fork contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""Succession plans: pass a user's role and branches on after their death.

A plan links a user account to their person in the tree and names an
ordered list of successors. Once a death is recorded for that person, the
plan waits for a number of confirmations by other editors, then for a grace
period during which the user can still cancel it, and finally hands the
user's role and branches to the first eligible successor, disables the
user's account and opens a plan for the successor with the remaining
successors, so the chain continues.

This module holds the data model and the state machine; detecting the
death in the tree and sending e-mails is done in `gramps_webapi.api.succession`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from . import User, user_db
from .branches import (
    belongs_to_tree,
    get_user_branches,
    set_user_branches,
    users_of_tree,
)
from .const import ROLE_DISABLED, ROLE_EDITOR, ROLE_GUEST
from .sql_guid import GUID

STATE_ACTIVE = "active"  # nothing happened
STATE_PENDING = "pending"  # death recorded, waiting for confirmations
STATE_CONFIRMED = "confirmed"  # confirmed, waiting for the grace period to end
STATE_EXECUTED = "executed"  # role handed over

OPEN_STATES = (STATE_ACTIVE, STATE_PENDING, STATE_CONFIRMED)

# events reported by `evaluate_plan`
EVENT_PENDING = "pending"
EVENT_REVERTED = "reverted"
EVENT_CONFIRMED = "confirmed"
EVENT_EXECUTED = "executed"
EVENT_NO_SUCCESSOR = "no_successor"


class SuccessionPlan(user_db.Model):  # type: ignore
    """Succession plan of a user."""

    __tablename__ = "succession_plans"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    tree: Mapped[str] = mapped_column(sa.String, nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # the user's person in the tree, whose death triggers the plan
    person_handle: Mapped[str | None] = mapped_column(sa.String)
    required_confirmations: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=2
    )
    grace_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=7)
    state: Mapped[str] = mapped_column(sa.String, nullable=False, default=STATE_ACTIVE)
    death_recorded_at: Mapped[datetime | None] = mapped_column(sa.DateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(sa.DateTime)
    execute_after: Mapped[datetime | None] = mapped_column(sa.DateTime)
    executed_at: Mapped[datetime | None] = mapped_column(sa.DateTime)
    executed_successor_id: Mapped[uuid.UUID | None] = mapped_column(GUID)
    # when the user last declared themselves alive: a death recorded before
    # that is ignored until the person record changes again
    vetoed_at: Mapped[datetime | None] = mapped_column(sa.DateTime)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime, nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, nullable=False, server_default=sa.func.now()
    )

    def __repr__(self):
        """Return string representation of instance."""
        return f"<SuccessionPlan(id={self.id}, user_id='{self.user_id}', state='{self.state}')>"


class SuccessionSuccessor(user_db.Model):  # type: ignore
    """A successor named in a plan, in order of preference."""

    __tablename__ = "succession_successors"

    plan_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("succession_plans.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


class SuccessionConfirmation(user_db.Model):  # type: ignore
    """A user's confirmation that the death recorded for a plan is real."""

    __tablename__ = "succession_confirmations"

    plan_id: Mapped[int] = mapped_column(
        sa.Integer,
        sa.ForeignKey("succession_plans.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime, nullable=False, server_default=sa.func.now()
    )


def utcnow() -> datetime:
    """Return the current UTC time without time zone, like the DB columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _session():
    return user_db.session  # pylint: disable=no-member


def get_plan(plan_id: int) -> Optional[SuccessionPlan]:
    """Return a plan by ID."""
    return _session().query(SuccessionPlan).filter_by(id=plan_id).scalar()


def get_plans(tree: str, include_executed: bool = False) -> List[SuccessionPlan]:
    """Return the plans of a tree, oldest first."""
    query = _session().query(SuccessionPlan).filter_by(tree=tree)
    if not include_executed:
        query = query.filter(SuccessionPlan.state.in_(OPEN_STATES))
    return query.order_by(SuccessionPlan.id).all()


def get_open_plan(user_id) -> Optional[SuccessionPlan]:
    """Return the user's plan that has not been executed yet."""
    return (
        _session()
        .query(SuccessionPlan)
        .filter_by(user_id=user_id)
        .filter(SuccessionPlan.state.in_(OPEN_STATES))
        .order_by(SuccessionPlan.id.desc())
        .first()
    )


def get_trees_with_open_plans() -> List[str]:
    """Return the IDs of all trees that have plans still to be checked."""
    query = (
        _session()
        .query(SuccessionPlan.tree)
        .filter(SuccessionPlan.state.in_(OPEN_STATES))
        .distinct()
    )
    return [row[0] for row in query.all()]


def get_plans_for_people(
    tree: str, person_handles: Iterable[str]
) -> List[SuccessionPlan]:
    """Return the open plans of a tree linked to any of the given people."""
    handles = list(person_handles)
    if not handles:
        return []
    return (
        _session()
        .query(SuccessionPlan)
        .filter_by(tree=tree)
        .filter(SuccessionPlan.state.in_(OPEN_STATES))
        .filter(SuccessionPlan.person_handle.in_(handles))
        .all()
    )


def create_plan(
    tree: str,
    user_id,
    person_handle: Optional[str],
    successor_ids: Iterable,
    required_confirmations: int = 2,
    grace_days: int = 7,
) -> SuccessionPlan:
    """Create a plan."""
    session = _session()
    now = utcnow()
    plan = SuccessionPlan(
        tree=tree,
        user_id=user_id,
        person_handle=person_handle or None,
        required_confirmations=required_confirmations,
        grace_days=grace_days,
        state=STATE_ACTIVE,
        created_at=now,
        updated_at=now,
    )
    session.add(plan)
    session.flush()  # get the ID
    _set_successors(plan, successor_ids)
    session.commit()
    return plan


def update_plan(
    plan: SuccessionPlan,
    person_handle: Optional[str] = None,
    successor_ids: Optional[Iterable] = None,
    required_confirmations: Optional[int] = None,
    grace_days: Optional[int] = None,
    clear_person: bool = False,
) -> None:
    """Change a plan's settings."""
    if person_handle is not None or clear_person:
        new_handle = None if clear_person else person_handle
        if new_handle != plan.person_handle:
            plan.person_handle = new_handle
            # a different person: start over
            _reset(plan)
    if successor_ids is not None:
        _set_successors(plan, successor_ids)
    if required_confirmations is not None:
        plan.required_confirmations = required_confirmations
    if grace_days is not None:
        plan.grace_days = grace_days
    plan.updated_at = utcnow()
    _session().commit()


def delete_plan(plan: SuccessionPlan) -> None:
    """Delete a plan."""
    session = _session()
    _delete_plan_rows(plan)
    session.delete(plan)
    session.commit()


def _delete_plan_rows(plan: SuccessionPlan) -> None:
    # SQLite does not enforce foreign key constraints by default
    session = _session()
    session.query(SuccessionSuccessor).filter_by(plan_id=plan.id).delete()
    session.query(SuccessionConfirmation).filter_by(plan_id=plan.id).delete()


def delete_user_succession(user_id) -> None:
    """Remove a user from all plans (without committing)."""
    session = _session()
    for plan in session.query(SuccessionPlan).filter_by(user_id=user_id).all():
        _delete_plan_rows(plan)
        session.delete(plan)
    session.query(SuccessionSuccessor).filter_by(user_id=user_id).delete()
    session.query(SuccessionConfirmation).filter_by(user_id=user_id).delete()


def _set_successors(plan: SuccessionPlan, successor_ids: Iterable) -> None:
    session = _session()
    session.query(SuccessionSuccessor).filter_by(plan_id=plan.id).delete()
    for position, user_id in enumerate(dict.fromkeys(successor_ids)):
        session.add(
            SuccessionSuccessor(plan_id=plan.id, position=position, user_id=user_id)
        )


def get_successors(plan: SuccessionPlan) -> List[User]:
    """Return the successors of a plan in order of preference."""
    query = (
        _session()
        .query(User)
        .join(SuccessionSuccessor, SuccessionSuccessor.user_id == User.id)
        .filter(SuccessionSuccessor.plan_id == plan.id)
        .order_by(SuccessionSuccessor.position)
    )
    return query.all()


def get_confirmations(plan: SuccessionPlan) -> List[tuple[User, datetime]]:
    """Return who confirmed a plan's death record, and when."""
    query = (
        _session()
        .query(User, SuccessionConfirmation.created_at)
        .join(SuccessionConfirmation, SuccessionConfirmation.user_id == User.id)
        .filter(SuccessionConfirmation.plan_id == plan.id)
        .order_by(SuccessionConfirmation.created_at)
    )
    return [(user, created_at) for user, created_at in query.all()]


def get_eligible_confirmers(plan: SuccessionPlan) -> List[User]:
    """Return the users who may confirm a plan: the tree's other editors and up.

    In single-tree mode, the tree's users include those without a tree ID.
    """
    query = (
        _session()
        .query(User)
        .filter(users_of_tree(plan.tree))
        .filter(User.role >= ROLE_EDITOR)
        .filter(User.id != plan.user_id)
        .order_by(User.name)
    )
    return query.all()


def get_successor_candidates(tree: str, exclude_user_id) -> List[User]:
    """Return the users of a tree who can be named as successors.

    Every enabled account of the tree qualifies, whatever its role, except
    the given one (a user cannot succeed themselves); disabled and
    unconfirmed accounts do not. In single-tree mode, the tree's users
    include those without a tree ID.
    """
    query = (
        _session()
        .query(User)
        .filter(users_of_tree(tree))
        .filter(User.role >= ROLE_GUEST)
        .filter(User.id != exclude_user_id)
        .order_by(User.name)
    )
    return query.all()


def can_confirm(plan: SuccessionPlan, user_id) -> bool:
    """Whether a user may (still) confirm a plan."""
    if plan.state != STATE_PENDING:
        return False
    if not any(str(user.id) == str(user_id) for user in get_eligible_confirmers(plan)):
        return False
    return not any(
        str(user.id) == str(user_id) for user, _created_at in get_confirmations(plan)
    )


def add_confirmation(plan: SuccessionPlan, user_id) -> None:
    """Record a user's confirmation."""
    session = _session()
    session.add(
        SuccessionConfirmation(plan_id=plan.id, user_id=user_id, created_at=utcnow())
    )
    session.commit()


def cancel_plan(plan: SuccessionPlan, now: Optional[datetime] = None) -> None:
    """Veto a plan: the user is alive. Back to the start."""
    now = now or utcnow()
    _reset(plan)
    plan.vetoed_at = now
    plan.updated_at = now
    _session().commit()


def _reset(plan: SuccessionPlan) -> None:
    plan.state = STATE_ACTIVE
    plan.death_recorded_at = None
    plan.confirmed_at = None
    plan.execute_after = None
    _session().query(SuccessionConfirmation).filter_by(plan_id=plan.id).delete()


def evaluate_plan(
    plan: SuccessionPlan,
    dead: bool,
    person_change: Optional[int] = None,
    now: Optional[datetime] = None,
) -> List[str]:
    """Advance a plan's state and return the events that happened.

    `dead` says whether a death is recorded for the plan's person,
    `person_change` is the person record's last change (Unix time): a death
    recorded before the user vetoed the plan does not count.
    """
    now = now or utcnow()
    if plan.state == STATE_EXECUTED:
        return []
    if dead and plan.vetoed_at is not None and person_change is not None:
        changed_at = datetime.fromtimestamp(person_change, tz=timezone.utc).replace(
            tzinfo=None
        )
        if changed_at <= plan.vetoed_at:
            dead = False
    events: List[str] = []
    if not dead:
        if plan.state != STATE_ACTIVE:
            _reset(plan)
            events.append(EVENT_REVERTED)
    else:
        if plan.state == STATE_ACTIVE:
            plan.state = STATE_PENDING
            plan.death_recorded_at = now
            events.append(EVENT_PENDING)
        if (
            plan.state == STATE_PENDING
            and len(get_confirmations(plan)) >= plan.required_confirmations
        ):
            plan.state = STATE_CONFIRMED
            plan.confirmed_at = now
            plan.execute_after = now + timedelta(days=plan.grace_days)
            events.append(EVENT_CONFIRMED)
        if plan.state == STATE_CONFIRMED and now >= plan.execute_after:
            successor = execute_plan(plan, now)
            events.append(EVENT_EXECUTED if successor else EVENT_NO_SUCCESSOR)
    if events:
        plan.updated_at = now
        _session().commit()
    return events


def execute_plan(
    plan: SuccessionPlan, now: Optional[datetime] = None
) -> Optional[User]:
    """Hand the user's role and branches to the first eligible successor.

    Returns the successor, or None if no successor is eligible (the plan is
    then left as it is).
    """
    now = now or utcnow()
    session = _session()
    user = session.query(User).filter_by(id=plan.user_id).one()
    successors = get_successors(plan)
    successor = next(
        (
            candidate
            for candidate in successors
            if candidate.id != user.id
            and (candidate.role or 0) >= 0
            and belongs_to_tree(candidate.tree, plan.tree)
        ),
        None,
    )
    if successor is None:
        return None
    successor.role = max(successor.role or 0, user.role or 0)
    branches = get_user_branches(successor.id) + get_user_branches(user.id)
    set_user_branches(successor.id, branches)
    # the account must not be used by anyone else
    user.role = ROLE_DISABLED
    plan.state = STATE_EXECUTED
    plan.executed_at = now
    plan.executed_successor_id = successor.id
    plan.updated_at = now
    session.commit()
    # the chain continues with the remaining successors
    if get_open_plan(successor.id) is None:
        remaining = [
            candidate.id
            for candidate in successors
            if candidate.id not in (user.id, successor.id)
        ]
        create_plan(
            tree=plan.tree,
            user_id=successor.id,
            person_handle=None,
            successor_ids=remaining,
            required_confirmations=plan.required_confirmations,
            grace_days=plan.grace_days,
        )
    return successor


def _user_dict(user: Optional[User]) -> Optional[Dict[str, Any]]:
    if user is None:
        return None
    return {"name": user.name, "full_name": user.fullname, "role": user.role}


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat()


def plan_to_dict(plan: SuccessionPlan, viewer_id=None) -> Dict[str, Any]:
    """Return a plan as a JSONifiable dict."""
    session = _session()
    user = session.query(User).filter_by(id=plan.user_id).scalar()
    successor = None
    if plan.executed_successor_id is not None:
        successor = (
            session.query(User).filter_by(id=plan.executed_successor_id).scalar()
        )
    return {
        "id": plan.id,
        "tree": plan.tree,
        "user": _user_dict(user),
        "person_handle": plan.person_handle,
        "required_confirmations": plan.required_confirmations,
        "grace_days": plan.grace_days,
        "state": plan.state,
        "death_recorded_at": _isoformat(plan.death_recorded_at),
        "confirmed_at": _isoformat(plan.confirmed_at),
        "execute_after": _isoformat(plan.execute_after),
        "executed_at": _isoformat(plan.executed_at),
        "executed_successor": _user_dict(successor),
        "vetoed_at": _isoformat(plan.vetoed_at),
        "successors": [_user_dict(candidate) for candidate in get_successors(plan)],
        "confirmations": [
            {"user": _user_dict(confirmer), "created_at": _isoformat(created_at)}
            for confirmer, created_at in get_confirmations(plan)
        ],
        "eligible_confirmers": [
            _user_dict(confirmer) for confirmer in get_eligible_confirmers(plan)
        ],
        "is_own": viewer_id is not None and str(plan.user_id) == str(viewer_id),
        "can_confirm": viewer_id is not None and can_confirm(plan, viewer_id),
    }


def is_plan_visible(plan: SuccessionPlan, viewer_id) -> bool:
    """Whether a user without user management rights may see a plan."""
    viewer = str(viewer_id)
    if str(plan.user_id) == viewer:
        return True
    if any(str(user.id) == viewer for user in get_successors(plan)):
        return True
    return any(str(user.id) == viewer for user in get_eligible_confirmers(plan))
