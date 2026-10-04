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

"""Succession: detect recorded deaths, advance the plans, notify people.

The data model and state machine live in `gramps_webapi.auth.succession`.
Plans are checked

- right after a transaction that changed a person a plan is linked to
  (`install_succession_hook`), so a recorded death is picked up at once,
- whenever the plans are listed or confirmed through the API,
- now and then on any request (`should_check_now`), for the time-based
  step from confirmed to executed,
- from the command line (`gramps_webapi succession check`), e.g. by cron.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from gettext import gettext as _
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import current_app
from gramps.gen.db import DbTxn
from gramps.gen.db.dbconst import PERSON_KEY, TXNADD, TXNDEL, TXNUPD
from gramps.gen.display.name import displayer as name_displayer
from gramps.gen.errors import HandleError
from gramps.gen.utils.db import get_death_or_fallback

from ..auth import User, get_owner_emails, user_db
from ..auth.branches import is_single_tree
from ..auth.succession import (
    EVENT_CONFIRMED,
    EVENT_EXECUTED,
    EVENT_NO_SUCCESSOR,
    EVENT_PENDING,
    SuccessionPlan,
    evaluate_plan,
    get_confirmations,
    get_eligible_confirmers,
    get_plans,
    get_plans_for_people,
    get_successors,
    get_trees_with_open_plans,
)

_LOG = logging.getLogger(__name__)

# where the frontend shows the plans
SUCCESSION_PATH = "/settings/succession"

_last_check: float = 0.0


def should_check_now(interval: float) -> bool:
    """Whether the periodic check is due; remembers the call."""
    global _last_check  # pylint: disable=global-statement
    now = time.monotonic()
    if now - _last_check < interval:
        return False
    _last_check = now
    return True


def person_death_status(db, handle: str) -> Optional[Tuple[bool, int]]:
    """Whether a death (or burial etc.) is recorded for a person, and the record's last change."""
    try:
        person = db.get_person_from_handle(handle)
    except HandleError:
        return None
    return get_death_or_fallback(db, person) is not None, person.change


def evaluate_tree(
    tree: str, now: Optional[datetime] = None
) -> List[Tuple[SuccessionPlan, List[str]]]:
    """Advance all open plans of a tree; return the ones that changed with their events."""
    plans = get_plans(tree)
    if not plans:
        return []
    # imported here: `util` imports this module
    from .util import close_db, get_db_outside_request

    # private records must be visible, else a private person reads as alive
    db = get_db_outside_request(tree=tree, view_private=True, readonly=True, user_id="")
    results = []
    try:
        for plan in plans:
            status = (
                person_death_status(db, plan.person_handle)
                if plan.person_handle
                else None
            )
            dead, change = status if status else (False, None)
            events = evaluate_plan(plan, dead=dead, person_change=change, now=now)
            if events:
                results.append((plan, events))
                _notify(plan, events, db)
    finally:
        close_db(db)
    return results


def evaluate_all_trees(
    now: Optional[datetime] = None,
) -> List[Tuple[SuccessionPlan, List[str]]]:
    """Advance the open plans of all trees."""
    results = []
    for tree in get_trees_with_open_plans():
        results.extend(evaluate_tree(tree, now=now))
    return results


# detecting changes to the people the plans are linked to


def install_succession_hook(db, tree: str) -> None:
    """Check the plans linked to the people a transaction changed, once it is committed."""
    if getattr(db, "succession_hook", False):
        return
    orig_transaction_commit = db.transaction_commit

    def transaction_commit(transaction: DbTxn):
        # collected before the commit, which clears the transaction
        handles = _person_handles(transaction)
        result = orig_transaction_commit(transaction)
        if handles:
            try:
                _people_changed(tree, handles)
            except Exception:  # pylint: disable=broad-except
                # never fail the user's write over this
                _LOG.exception("Succession check after transaction failed")
        return result

    db.transaction_commit = transaction_commit
    db.succession_hook = True


def _person_handles(transaction: DbTxn) -> set:
    handles = set()
    for trans_type in (TXNADD, TXNUPD, TXNDEL):
        for handle, _data in transaction.get((PERSON_KEY, trans_type), []):
            handles.add(handle)
    return handles


def _people_changed(tree: str, handles: Iterable[str]) -> None:
    if not get_plans_for_people(tree, handles):
        return
    from .tasks import check_succession, run_task  # avoid an import cycle

    run_task(check_succession, tree=tree)


# notifications


def _notify(plan: SuccessionPlan, events: Sequence[str], db) -> None:
    from .util import get_config  # avoid an import cycle

    user = user_db.session.query(User).filter_by(id=plan.user_id).scalar()
    if user is None:
        return
    person_name = _person_name(db, plan.person_handle)
    base_url = (get_config("BASE_URL") or "").rstrip("/")
    context = {
        "user": _display_user(user),
        "person": person_name or _display_user(user),
        "link": f"{base_url}{SUCCESSION_PATH}",
        "required": plan.required_confirmations,
        "grace_days": plan.grace_days,
    }
    for event in events:
        message = _compose(event, plan, user, context)
        if message is None:
            continue
        subject, body, recipients = message
        _send(subject, body, recipients)


def _compose(
    event: str, plan: SuccessionPlan, user: User, context: Dict[str, Any]
) -> Optional[Tuple[str, str, List[str]]]:
    confirmers = get_eligible_confirmers(plan)
    confirmed_by = [confirmer for confirmer, _created_at in get_confirmations(plan)]
    successors = get_successors(plan)
    if event == EVENT_PENDING:
        subject = _("Succession: death recorded for %(user)s") % context
        body = (
            _(
                "A death has been recorded in the family tree for %(person)s, "
                "the person linked to the account of %(user)s."
            )
            % context
            + "\n\n"
            + _(
                "If this is correct, please confirm it here: %(link)s\n"
                "The account's role will be handed to a successor once "
                "%(required)d confirmation(s) have been given and a grace period of "
                "%(grace_days)d day(s) has passed."
            )
            % context
            + "\n\n"
            + _(
                "If you are %(user)s and this is a mistake, log in and cancel "
                "the succession plan: %(link)s"
            )
            % context
        )
        recipients = _emails(confirmers + [user])
    elif event == EVENT_CONFIRMED:
        context = {
            **context,
            "execute_after": (
                plan.execute_after.strftime("%Y-%m-%d %H:%M UTC")
                if plan.execute_after
                else ""
            ),
            "successors": ", ".join(_display_user(s) for s in successors) or "-",
        }
        subject = _("Succession: handover for %(user)s confirmed") % context
        body = (
            _(
                "The death recorded for %(person)s has been confirmed. "
                "The role of the account %(user)s will be handed to the first "
                "available successor (%(successors)s) after %(execute_after)s."
            )
            % context
            + "\n\n"
            + _(
                "If you are %(user)s and this is a mistake, log in and cancel "
                "the succession plan: %(link)s"
            )
            % context
        )
        recipients = _emails([user] + successors + confirmed_by)
    elif event == EVENT_EXECUTED:
        successor = (
            user_db.session.query(User)
            .filter_by(id=plan.executed_successor_id)
            .scalar()
        )
        context = {**context, "successor": _display_user(successor)}
        subject = _("Succession: %(successor)s took over from %(user)s") % context
        body = (
            _(
                "The role of the account %(user)s has been handed to %(successor)s, "
                "and the account %(user)s has been disabled."
            )
            % context
            + "\n\n"
            + _(
                "%(successor)s: please set up your own succession plan, "
                "naming the people who should take over from you: %(link)s"
            )
            % context
        )
        recipients = _emails([successor] + confirmed_by) + _owner_emails(plan)
    elif event == EVENT_NO_SUCCESSOR:
        subject = _("Succession: no successor available for %(user)s") % context
        body = (
            _(
                "The succession plan of %(user)s is due, but none of the named "
                "successors can take over (their accounts are missing, disabled "
                "or belong to another tree). Please appoint a new owner: %(link)s"
            )
            % context
        )
        recipients = _owner_emails(plan)
    else:
        return None
    return subject, body, sorted(set(recipients))


def _owner_emails(plan: SuccessionPlan) -> List[str]:
    """Return the e-mail addresses of the tree's owners and admins.

    In single-tree mode, owners and admins without a tree ID belong to the
    tree too.
    """
    return get_owner_emails(
        plan.tree, include_admins=True, include_treeless=is_single_tree()
    )


def _emails(users: Iterable[Optional[User]]) -> List[str]:
    return [user.email for user in users if user is not None and user.email]


def _display_user(user: Optional[User]) -> str:
    if user is None:
        return "-"
    return user.fullname or user.name


def _person_name(db, handle: Optional[str]) -> Optional[str]:
    if not handle:
        return None
    try:
        person = db.get_person_from_handle(handle)
    except HandleError:
        return None
    return name_displayer.display(person)


def _send(subject: str, body: str, recipients: List[str]) -> None:
    if not recipients:
        return
    from .util import send_email  # avoid an import cycle

    try:
        send_email(subject=subject, body=body, to=recipients)
    except Exception:  # pylint: disable=broad-except
        # e-mail may not be configured; the plan still advances
        _LOG.warning("Could not send succession e-mail '%s'", subject, exc_info=True)


def auto_cancel_on_login(user_id) -> None:
    """A user logging in with their password is alive: cancel their running plan."""
    from ..auth.succession import STATE_ACTIVE, cancel_plan, get_open_plan

    plan = get_open_plan(user_id)
    if plan is not None and plan.state != STATE_ACTIVE:
        _LOG.info("Succession plan %s cancelled: %s logged in", plan.id, user_id)
        cancel_plan(plan)
