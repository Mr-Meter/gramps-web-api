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

"""Branches: restrict a user's write access to parts of a tree.

A branch is a Gramps tag. Users below the owner role who are assigned to
one or more branches may only change objects carrying one of those tags
(enforced by `gramps_webapi.branch_guard`). Users without branches, and
owners and admins, are unrestricted.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import sqlalchemy as sa
from flask import current_app, has_app_context
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.functions import coalesce

from ..const import TREE_MULTI
from . import User, user_db
from .const import ROLE_OWNER
from .sql_guid import GUID


class UserBranch(user_db.Model):  # type: ignore
    """A branch (Gramps tag) a user's write access is restricted to."""

    __tablename__ = "user_branches"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    tag_handle: Mapped[str] = mapped_column(sa.String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime, nullable=False, server_default=sa.func.now()
    )

    def __repr__(self):
        """Return string representation of instance."""
        return f"<UserBranch(user_id='{self.user_id}', tag='{self.tag_handle}')>"


def get_user_branches(user_id) -> List[str]:
    """Return the branch tag handles of a user."""
    query = (
        user_db.session.query(UserBranch.tag_handle)  # pylint: disable=no-member
        .filter_by(user_id=user_id)
        .order_by(UserBranch.created_at, UserBranch.tag_handle)
    )
    return [row[0] for row in query.all()]


def get_users_branches(user_ids: Iterable) -> Dict[uuid.UUID, List[str]]:
    """Return the branch tag handles of several users, keyed by user ID."""
    user_ids = list(user_ids)
    branches: Dict[uuid.UUID, List[str]] = {user_id: [] for user_id in user_ids}
    if not user_ids:
        return branches
    query = (
        user_db.session.query(UserBranch)  # pylint: disable=no-member
        .filter(UserBranch.user_id.in_(user_ids))
        .order_by(UserBranch.created_at, UserBranch.tag_handle)
    )
    for row in query.all():
        branches.setdefault(row.user_id, []).append(row.tag_handle)
    return branches


def set_user_branches(user_id, tag_handles: Iterable[str]) -> None:
    """Replace the branches of a user."""
    session = user_db.session  # pylint: disable=no-member
    wanted = list(dict.fromkeys(tag_handles))  # unique, order kept
    existing = {
        row.tag_handle: row
        for row in session.query(UserBranch).filter_by(user_id=user_id).all()
    }
    for tag_handle, row in existing.items():
        if tag_handle not in wanted:
            session.delete(row)
    for tag_handle in wanted:
        if tag_handle not in existing:
            session.add(UserBranch(user_id=user_id, tag_handle=tag_handle))
    session.commit()


def delete_user_branches(user_id) -> None:
    """Remove all branch assignments of a user (without committing)."""
    user_db.session.query(UserBranch).filter_by(  # pylint: disable=no-member
        user_id=user_id
    ).delete()


def get_branch_scope(user_id) -> Optional[frozenset[str]]:
    """Return the tag handles a user's writes are restricted to.

    Returns None if the user is unrestricted: owners, admins, users without
    branch assignments - and unknown users, who cannot write anyway.
    """
    if not user_id:
        return None
    try:
        user = (
            user_db.session.query(User)  # pylint: disable=no-member
            .filter_by(id=user_id)
            .scalar()
        )
    except sa.exc.StatementError:
        return None
    if user is None or (user.role or 0) >= ROLE_OWNER:
        return None
    branches = get_user_branches(user.id)
    if not branches:
        return None
    return frozenset(branches)


# tree membership


def is_single_tree() -> bool:
    """Whether the app serves a single tree.

    In a single-tree deployment, users without a tree ID (e.g. created with
    `gramps_webapi user add` without `--tree`) are members of the one tree,
    as for `get_all_user_details(include_treeless=True)`. In multi-tree mode
    they are site admins tied to no tree. Outside an app context the strict
    multi-tree reading applies.
    """
    return has_app_context() and current_app.config.get("TREE") != TREE_MULTI


def users_of_tree(tree: str):
    """Return the SQL condition for the users who belong to a tree.

    In single-tree mode this includes the users without a tree ID (NULL or
    "", treated alike as by `fill_tree`), see `is_single_tree`.
    """
    if is_single_tree():
        return sa.or_(User.tree == tree, coalesce(User.tree, "") == "")
    return User.tree == tree


def belongs_to_tree(user_tree: Optional[str], tree: str) -> bool:
    """Whether a user with the given tree ID belongs to a tree.

    The Python counterpart of `users_of_tree`: a user without a tree ID
    belongs to the tree in single-tree mode only.
    """
    if user_tree == tree:
        return True
    return not user_tree and is_single_tree()


def get_tree_branches(tree: str) -> Dict[str, List[User]]:
    """Return the users assigned to each branch tag in a tree.

    In single-tree mode, the tree's users include those without a tree ID.
    """
    query = (
        user_db.session.query(UserBranch, User)  # pylint: disable=no-member
        .join(User, User.id == UserBranch.user_id)
        .filter(users_of_tree(tree))
        .order_by(UserBranch.tag_handle, User.name)
    )
    branches: Dict[str, List[User]] = {}
    for row, user in query.all():
        branches.setdefault(row.tag_handle, []).append(user)
    return branches
