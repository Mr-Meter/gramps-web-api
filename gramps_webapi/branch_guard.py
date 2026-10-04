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

"""Restrict database writes to the branches a user administers.

A branch is a Gramps tag. A user whose write access is scoped to branches
(see `gramps_webapi.auth.branches`) may only change or delete objects that
carry one of their branch tags, and objects they create get their branch
tags. The guard hooks into the low-level commit and remove methods of the
database handle, so it covers every write path - object endpoints, raw
transactions, merges, deletions with reference cleanup - without each
endpoint having to check for itself.

Objects of other branches may still be *linked* to one's own: a marriage
between two branches changes the other branch's person (its family list),
and that must not require editing rights on that person. Such changes are
allowed as long as nothing but the link fields in `LINK_FIELDS` changes and
every handle added or removed there belongs to the user's own branch.
"""

from __future__ import annotations

import logging
from gettext import gettext as _
from typing import Any, Dict, Iterable, Optional

from gramps.gen.db import DbTxn
from gramps.gen.db.dbconst import KEY_TO_CLASS_MAP
from gramps.gen.errors import HandleError
from gramps.gen.lib.json_utils import object_to_dict

_LOG = logging.getLogger(__name__)

# Fields of an object of another branch that may change, and the class of
# the objects they refer to. Every handle added to or removed from them must
# belong to the user's own branch.
LINK_FIELDS: Dict[str, Dict[str, str]] = {
    "Person": {
        "family_list": "Family",
        "parent_family_list": "Family",
        "person_ref_list": "Person",
    },
    "Family": {
        "father_handle": "Person",
        "mother_handle": "Person",
        "child_ref_list": "Person",
    },
}


class BranchPermissionError(Exception):
    """A write outside the user's branches."""


class BranchGuard:
    """Check every commit and removal on a database handle against a branch scope."""

    def __init__(self, db, tag_handles: Iterable[str]) -> None:
        self.db = db
        self.tag_handles = frozenset(tag_handles)
        # handles of own objects removed in the current transaction: references
        # to them may be dropped from objects of other branches
        self._removed: set[str] = set()
        self._orig_commit_base = db._commit_base
        self._orig_do_remove = db._do_remove
        self._orig_transaction_begin = db.transaction_begin

    def install(self) -> None:
        """Hook into the database handle."""
        self.db._commit_base = self._commit_base
        self.db._do_remove = self._do_remove
        self.db.transaction_begin = self._transaction_begin
        self.db.branch_guard = self

    # hooks

    def _transaction_begin(self, transaction: DbTxn):
        self._removed.clear()
        return self._orig_transaction_begin(transaction)

    def _commit_base(self, obj, obj_key, trans, change_time):
        class_name = KEY_TO_CLASS_MAP.get(obj_key)
        if class_name == "Tag":
            # new tags are harmless; existing ones may be branch tags
            if self._get(class_name, obj.handle) is not None:
                raise BranchPermissionError(
                    _("Tags can only be changed by an editor or owner.")
                )
        elif class_name is not None and obj.handle:
            old_obj = self._get(class_name, obj.handle)
            if old_obj is None:
                if not self._owns(obj):
                    for handle in sorted(self.tag_handles):
                        obj.add_tag(handle)
            elif not self._owns(old_obj):
                self._check_foreign_update(class_name, old_obj, obj)
        return self._orig_commit_base(obj, obj_key, trans, change_time)

    def _do_remove(self, handle, transaction, obj_key):
        class_name = KEY_TO_CLASS_MAP.get(obj_key)
        if class_name == "Tag":
            raise BranchPermissionError(
                _("Tags can only be deleted by an editor or owner.")
            )
        if class_name is not None and handle:
            obj = self._get(class_name, handle)
            if obj is not None:
                if not self._owns(obj):
                    raise BranchPermissionError(
                        self._message(class_name, obj, _("deleted"))
                    )
                self._removed.add(handle)
        return self._orig_do_remove(handle, transaction, obj_key)

    # helpers

    def _owns(self, obj) -> bool:
        return bool(set(obj.get_tag_list()) & self.tag_handles)

    def _get(self, class_name: str, handle: str):
        try:
            return self.db.method("get_%s_from_handle", class_name)(handle)
        except HandleError:
            return None

    def _is_own_handle(self, class_name: str, handle: str) -> bool:
        if handle in self._removed:
            return True
        obj = self._get(class_name, handle)
        if obj is None:
            # not in the database (yet): it is being created in this
            # transaction, and anything created here gets the user's tags
            return True
        return self._owns(obj)

    @staticmethod
    def _message(class_name: str, obj, verb: str, field: Optional[str] = None) -> str:
        message = _(
            "%(type)s %(id)s belongs to another branch and cannot be %(verb)s."
        ) % {
            "type": _(class_name),
            "id": obj.gramps_id,
            "verb": verb,
        }
        if field:
            message += " " + _("(field: %s)") % field
        return message

    def _check_foreign_update(self, class_name: str, old_obj, new_obj) -> None:
        # object_to_dict, not object_to_data: the latter stashes the object
        # itself in the dict, which would make any two objects differ
        old = self._strip_removed(object_to_dict(old_obj))
        new = self._strip_removed(object_to_dict(new_obj))
        link_fields = LINK_FIELDS.get(class_name, {})
        for key in sorted(set(old) | set(new)):
            if key == "change" or old.get(key) == new.get(key):
                continue
            if key not in link_fields:
                raise BranchPermissionError(
                    self._message(class_name, old_obj, _("changed"), key)
                )
            self._check_link_change(
                class_name, old_obj, key, link_fields[key], old.get(key), new.get(key)
            )

    def _check_link_change(
        self, class_name: str, obj, field: str, ref_class: str, old_value, new_value
    ) -> None:
        old_refs = _ref_map(old_value)
        new_refs = _ref_map(new_value)
        for handle in old_refs.keys() ^ new_refs.keys():
            if not self._is_own_handle(ref_class, handle):
                raise BranchPermissionError(
                    self._message(class_name, obj, _("linked to another branch"), field)
                )
        for handle in old_refs.keys() & new_refs.keys():
            if old_refs[handle] != new_refs[handle]:
                raise BranchPermissionError(
                    self._message(class_name, obj, _("changed"), field)
                )

    def _strip_removed(self, value: Any) -> Any:
        """Drop references to objects removed in this transaction."""
        if not self._removed:
            return value
        if isinstance(value, dict):
            return {key: self._strip_removed(item) for key, item in value.items()}
        if isinstance(value, list):
            return [
                self._strip_removed(item)
                for item in value
                if not (
                    (isinstance(item, str) and item in self._removed)
                    or (isinstance(item, dict) and item.get("ref") in self._removed)
                )
            ]
        if isinstance(value, str) and value in self._removed:
            return None
        return value


def _ref_map(value: Any) -> Dict[str, Any]:
    """Map the handles referenced by a link field to their entries."""
    if not value:
        return {}
    if isinstance(value, str):
        return {value: value}
    refs: Dict[str, Any] = {}
    for item in value:
        if isinstance(item, dict):
            refs[item.get("ref")] = item
        else:
            refs[item] = item
    return refs


def install_branch_guard(db, tag_handles: Iterable[str]) -> BranchGuard:
    """Restrict writes through `db` to objects tagged with `tag_handles`."""
    guard = BranchGuard(db, tag_handles)
    guard.install()
    return guard


def get_branch_guard(db) -> Optional[BranchGuard]:
    """Return the guard installed on a database handle, if any."""
    return getattr(db, "branch_guard", None)
