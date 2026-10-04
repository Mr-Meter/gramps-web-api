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

"""Branches: which users administer which parts of the tree."""

from typing import Iterable

from flask import jsonify
from flask_jwt_extended import get_jwt_identity
from gramps.gen.errors import HandleError

from ...auth.branches import get_tree_branches
from ..blueprint import api_blueprint
from ..util import (
    abort_with_message,
    close_db,
    get_db_handle,
    get_db_outside_request,
    get_tree_from_jwt,
    get_tree_from_jwt_or_fail,
)
from . import ProtectedResource
from .schemas import BranchSchema


def validate_branch_handles(tree: str, handles: Iterable[str]) -> None:
    """Abort with 422 unless every handle is a tag of the tree."""
    handles = list(handles)
    if not handles:
        return
    if not tree:
        # a treeless site admin has no tree whose tags could be branches
        abort_with_message(422, "Tree is required to assign branches")
    if tree == get_tree_from_jwt():
        db = get_db_handle()
        opened = False
    else:
        # a site admin editing a user of another tree
        db = get_db_outside_request(
            tree=tree, view_private=True, readonly=True, user_id=get_jwt_identity()
        )
        opened = True
    try:
        for handle in handles:
            if not db.has_tag_handle(handle):
                abort_with_message(422, f"Tag {handle} does not exist")
    finally:
        if opened:
            close_db(db)


class BranchesResource(ProtectedResource):
    """The branches of the tree and the users administering them."""

    @api_blueprint.response(200, BranchSchema(many=True))
    def get(self):
        """List the branches (tags) of the tree with their administrators."""
        tree = get_tree_from_jwt_or_fail()
        db = get_db_handle()
        branches = []
        for tag_handle, users in get_tree_branches(tree).items():
            try:
                tag = db.get_tag_from_handle(tag_handle)
            except HandleError:
                tag = None  # the tag was deleted; the assignment is stale
            branches.append(
                {
                    "handle": tag_handle,
                    "name": tag.get_name() if tag else None,
                    "color": tag.get_color() if tag else None,
                    "users": [
                        {
                            "name": user.name,
                            "full_name": user.fullname,
                            "role": user.role,
                        }
                        for user in users
                    ],
                }
            )
        return jsonify(branches), 200
