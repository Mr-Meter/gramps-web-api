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

"""Succession plan resources."""

from typing import Any, Dict, Iterable, List, Optional

from flask import jsonify
from flask_jwt_extended import get_jwt_identity
from gramps.gen.display.name import displayer as name_displayer
from gramps.gen.errors import HandleError
from marshmallow import Schema, validate
from webargs import fields

from ...auth import get_guid, get_name, get_tree
from ...auth.branches import belongs_to_tree
from ...auth.const import PERM_EDIT_OBJ, PERM_EDIT_USER_ROLE
from ...auth.succession import (
    SuccessionPlan,
    add_confirmation,
    can_confirm,
    cancel_plan,
    create_plan,
    delete_plan,
    get_open_plan,
    get_plan,
    get_plans,
    get_successor_candidates,
    is_plan_visible,
    plan_to_dict,
    update_plan,
)
from ..auth import has_permissions, require_permissions
from ..blueprint import api_blueprint
from ..succession import evaluate_tree
from ..util import abort_with_message, get_db_handle, get_tree_from_jwt_or_fail
from . import ProtectedResource
from .schemas import (
    BranchUserSchema,
    SuccessionCheckResultSchema,
    SuccessionPlanSchema,
)


class SuccessionPlanPostArgs(Schema):
    """Body arguments for POST /succession/."""

    user = fields.Str(
        required=False,
        metadata={
            "description": "Username the plan is for. Defaults to the caller;"
            " naming someone else requires user management rights."
        },
    )
    person_handle = fields.Str(
        required=False,
        allow_none=True,
        metadata={"description": "Handle of the user's person in the tree."},
    )
    successors = fields.List(
        fields.Str(),
        load_default=[],
        metadata={
            "description": "Usernames of the successors, in order of preference."
        },
    )
    required_confirmations = fields.Int(
        load_default=2,
        validate=validate.Range(min=0, max=100),
        metadata={
            "description": "Number of other editors who must confirm the recorded death."
        },
    )
    grace_days = fields.Int(
        load_default=7,
        validate=validate.Range(min=0, max=3650),
        metadata={
            "description": "Days to wait after the confirmations before handing over."
        },
    )


class SuccessionPlanPutArgs(Schema):
    """Body arguments for PUT /succession/<id>/."""

    person_handle = fields.Str(
        required=False,
        allow_none=True,
        metadata={
            "description": "Handle of the user's person in the tree; null unlinks it."
        },
    )
    successors = fields.List(
        fields.Str(),
        required=False,
        metadata={
            "description": "Usernames of the successors, in order of preference."
        },
    )
    required_confirmations = fields.Int(
        required=False,
        validate=validate.Range(min=0, max=100),
        metadata={
            "description": "Number of other editors who must confirm the recorded death."
        },
    )
    grace_days = fields.Int(
        required=False,
        validate=validate.Range(min=0, max=3650),
        metadata={
            "description": "Days to wait after the confirmations before handing over."
        },
    )


class SuccessionCandidatesArgs(Schema):
    """Query arguments for GET /succession/candidates/."""

    user = fields.Str(
        required=False,
        metadata={
            "description": "Username whose successor candidates to list, i.e."
            " everyone but them. Defaults to the caller; naming someone else"
            " requires user management rights."
        },
    )


def _user_in_tree(user_name: str, tree: str):
    """Return the ID of a user of the tree or abort.

    In single-tree mode, users without a tree ID belong to the tree.
    """
    try:
        user_id = get_guid(user_name)
    except ValueError:
        abort_with_message(422, f"User {user_name} does not exist")
    if not belongs_to_tree(get_tree(user_id), tree):
        abort_with_message(422, f"User {user_name} does not belong to this tree")
    return user_id


def _resolve_successors(names: Iterable[str], tree: str, plan_user_id) -> List:
    user_ids = []
    for name in names:
        user_id = _user_in_tree(name, tree)
        if str(user_id) == str(plan_user_id):
            abort_with_message(422, "A user cannot be their own successor")
        user_ids.append(user_id)
    return user_ids


def _validate_person(handle):
    if handle and not get_db_handle().has_person_handle(handle):
        abort_with_message(422, "Person does not exist")


def _person_summary(db, handle: Optional[str]) -> Optional[Dict[str, str]]:
    """Return the Gramps ID and display name of a plan's person.

    None if the plan has no person, the person was deleted, or the caller
    may not see them (a private person behind the private proxy).
    """
    if not handle:
        return None
    try:
        person = db.get_person_from_handle(handle)
    except HandleError:
        return None
    if person is None:
        return None
    return {
        "gramps_id": person.get_gramps_id(),
        "name": name_displayer.display(person),
    }


def _plan_json(plan: SuccessionPlan, viewer) -> Dict[str, Any]:
    """Return a plan as JSON data, with a summary of the linked person.

    The person is looked up through the caller's database handle, so a
    private person is null for users who may not view private records.
    """
    data = plan_to_dict(plan, viewer)
    data["person"] = _person_summary(get_db_handle(), plan.person_handle)
    return data


def _get_plan_or_404(plan_id: int, tree: str) -> SuccessionPlan:
    plan = get_plan(plan_id)
    if plan is None or plan.tree != tree:
        abort_with_message(404, "Succession plan not found")
    return plan


def _require_manage(plan: SuccessionPlan) -> None:
    """The plan's own user or a user manager may change it."""
    if str(plan.user_id) != get_jwt_identity():
        require_permissions([PERM_EDIT_USER_ROLE])


class SuccessionPlansResource(ProtectedResource):
    """The succession plans of the tree."""

    @api_blueprint.response(200, SuccessionPlanSchema(many=True))
    def get(self):
        """List the succession plans, after checking them for recorded deaths."""
        tree = get_tree_from_jwt_or_fail()
        viewer = get_jwt_identity()
        evaluate_tree(tree)
        plans = get_plans(tree, include_executed=True)
        if not has_permissions([PERM_EDIT_USER_ROLE]):
            plans = [plan for plan in plans if is_plan_visible(plan, viewer)]
        return jsonify([_plan_json(plan, viewer) for plan in plans]), 200

    @api_blueprint.response(201, SuccessionPlanSchema())
    @api_blueprint.arguments(SuccessionPlanPostArgs, location="json")
    def post(self, args):
        """Create a succession plan for oneself or, as a user manager, for someone else."""
        tree = get_tree_from_jwt_or_fail()
        viewer = get_jwt_identity()
        if args.get("user"):
            user_id = _user_in_tree(args["user"], tree)
            if str(user_id) != viewer:
                require_permissions([PERM_EDIT_USER_ROLE])
        else:
            user_id = viewer
        if str(user_id) == viewer:
            # editors and up have something to hand over
            require_permissions([PERM_EDIT_OBJ])
        if get_open_plan(user_id) is not None:
            abort_with_message(409, "The user already has a succession plan")
        _validate_person(args.get("person_handle"))
        successor_ids = _resolve_successors(args["successors"], tree, user_id)
        plan = create_plan(
            tree=tree,
            user_id=user_id,
            person_handle=args.get("person_handle"),
            successor_ids=successor_ids,
            required_confirmations=args["required_confirmations"],
            grace_days=args["grace_days"],
        )
        return jsonify(_plan_json(plan, viewer)), 201


class SuccessionCandidatesResource(ProtectedResource):
    """Users who can be named as successors."""

    @api_blueprint.response(200, BranchUserSchema(many=True))
    @api_blueprint.arguments(SuccessionCandidatesArgs, location="query")
    def get(self, args):
        """List the tree's other enabled users, who can be named as successors."""
        require_permissions([PERM_EDIT_OBJ])
        tree = get_tree_from_jwt_or_fail()
        viewer = get_jwt_identity()
        exclude_user_id = viewer
        if args.get("user"):
            try:
                own_name = get_name(viewer)
            except ValueError:
                abort_with_message(401, "User not found for token ID")
            if args["user"] != own_name:
                # a user manager planning for someone else: everyone but them
                require_permissions([PERM_EDIT_USER_ROLE])
            exclude_user_id = _user_in_tree(args["user"], tree)
        candidates = get_successor_candidates(tree, exclude_user_id=exclude_user_id)
        return (
            jsonify(
                [
                    {"name": user.name, "full_name": user.fullname, "role": user.role}
                    for user in candidates
                ]
            ),
            200,
        )


class SuccessionPlanResource(ProtectedResource):
    """A single succession plan."""

    @api_blueprint.response(200, SuccessionPlanSchema())
    def get(self, plan_id: int):
        """Get a succession plan."""
        tree = get_tree_from_jwt_or_fail()
        viewer = get_jwt_identity()
        plan = _get_plan_or_404(plan_id, tree)
        if not has_permissions([PERM_EDIT_USER_ROLE]) and not is_plan_visible(
            plan, viewer
        ):
            abort_with_message(403, "Not authorized to view this succession plan")
        return jsonify(_plan_json(plan, viewer)), 200

    @api_blueprint.response(200, SuccessionPlanSchema())
    @api_blueprint.arguments(SuccessionPlanPutArgs, location="json")
    def put(self, args, plan_id: int):
        """Change a succession plan."""
        tree = get_tree_from_jwt_or_fail()
        viewer = get_jwt_identity()
        plan = _get_plan_or_404(plan_id, tree)
        _require_manage(plan)
        if "person_handle" in args:
            _validate_person(args["person_handle"])
        successor_ids = None
        if "successors" in args:
            successor_ids = _resolve_successors(args["successors"], tree, plan.user_id)
        update_plan(
            plan,
            person_handle=args.get("person_handle"),
            clear_person="person_handle" in args and not args["person_handle"],
            successor_ids=successor_ids,
            required_confirmations=args.get("required_confirmations"),
            grace_days=args.get("grace_days"),
        )
        evaluate_tree(tree)
        return jsonify(_plan_json(get_plan(plan_id), viewer)), 200

    def delete(self, plan_id: int):
        """Delete a succession plan."""
        tree = get_tree_from_jwt_or_fail()
        plan = _get_plan_or_404(plan_id, tree)
        _require_manage(plan)
        delete_plan(plan)
        return "", 200


class SuccessionConfirmResource(ProtectedResource):
    """Confirm the death recorded for a plan."""

    @api_blueprint.response(200, SuccessionPlanSchema())
    def post(self, plan_id: int):
        """Confirm that the death recorded for the plan's person is real."""
        tree = get_tree_from_jwt_or_fail()
        viewer = get_jwt_identity()
        plan = _get_plan_or_404(plan_id, tree)
        if not can_confirm(plan, viewer):
            abort_with_message(
                403,
                "Only other editors of the tree may confirm, once, "
                "while the plan is waiting for confirmations",
            )
        add_confirmation(plan, viewer)
        evaluate_tree(tree)
        return jsonify(_plan_json(get_plan(plan_id), viewer)), 200


class SuccessionCancelResource(ProtectedResource):
    """Cancel a running plan: the user is alive."""

    @api_blueprint.response(200, SuccessionPlanSchema())
    def post(self, plan_id: int):
        """Cancel a running succession plan; the plan starts over."""
        tree = get_tree_from_jwt_or_fail()
        viewer = get_jwt_identity()
        plan = _get_plan_or_404(plan_id, tree)
        _require_manage(plan)
        if plan.state == "executed":
            abort_with_message(409, "The plan has already been executed")
        cancel_plan(plan)
        return jsonify(_plan_json(get_plan(plan_id), viewer)), 200


class SuccessionCheckResource(ProtectedResource):
    """Run the succession check now."""

    @api_blueprint.response(200, SuccessionCheckResultSchema())
    def post(self):
        """Check the tree's plans for recorded deaths and due handovers now."""
        require_permissions([PERM_EDIT_USER_ROLE])
        tree = get_tree_from_jwt_or_fail()
        results = evaluate_tree(tree)
        return (
            jsonify(
                {
                    "plans": [
                        {"id": plan.id, "events": events} for plan, events in results
                    ]
                }
            ),
            200,
        )
