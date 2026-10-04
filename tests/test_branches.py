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

"""Tests for branch-scoped write access."""

import os
import unittest
import uuid
from copy import deepcopy
from typing import Dict
from unittest.mock import patch

from flask_jwt_extended import decode_token
from gramps.cli.clidbman import CLIDbManager
from gramps.gen.dbstate import DbState

from gramps_webapi.app import create_app
from gramps_webapi.auth import User, add_user, get_guid, user_db
from gramps_webapi.auth.branches import (
    belongs_to_tree,
    set_user_branches,
    users_of_tree,
)
from gramps_webapi.auth.const import ROLE_CONTRIBUTOR, ROLE_EDITOR, ROLE_OWNER
from gramps_webapi.const import ENV_CONFIG_FILE, TEST_AUTH_CONFIG, TREE_MULTI


def get_headers(client, user: str, password: str) -> Dict[str, str]:
    """Get the auth headers for a specific user."""
    rv = client.post("/api/token/", json={"username": user, "password": password})
    return {"Authorization": "Bearer {}".format(rv.json["access_token"])}


def make_handle() -> str:
    """Make a new valid handle."""
    return str(uuid.uuid4())


def person(first_name: str, surname: str, tags=None) -> dict:
    """Build a person payload."""
    return {
        "_class": "Person",
        "handle": make_handle(),
        "gender": 1,
        "primary_name": {
            "_class": "Name",
            "first_name": first_name,
            "surname_list": [{"_class": "Surname", "surname": surname}],
        },
        "tag_list": tags or [],
    }


class TestBranches(unittest.TestCase):
    """Branch admins may only write to their own branch."""

    @classmethod
    def setUpClass(cls):
        cls.name = "Test Branches"
        cls.dbman = CLIDbManager(DbState())
        dbpath, _ = cls.dbman.create_new_db_cli(cls.name, dbid="sqlite")
        cls.tree = os.path.basename(dbpath)
        with patch.dict("os.environ", {ENV_CONFIG_FILE: TEST_AUTH_CONFIG}):
            cls.app = create_app(
                config={"SUCCESSION_CHECK_INTERVAL": 0, "RATELIMIT_ENABLED": False},
                config_from_env=False,
            )
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            user_db.create_all()
            for name, role in [
                ("owner", ROLE_OWNER),
                ("editor", ROLE_EDITOR),
                ("alice", ROLE_EDITOR),  # branch A admin
                ("bob", ROLE_EDITOR),  # branch B admin
                ("carol", ROLE_CONTRIBUTOR),  # branch A contributor
                ("dave", ROLE_EDITOR),  # branches A and B
            ]:
                add_user(name=name, password="123", role=role, tree=cls.tree)
            # created with `gramps_webapi user add` without --tree: no tree ID
            add_user(name="nomad", password="123", role=ROLE_EDITOR, tree=None)
        cls.headers = {
            name: get_headers(cls.client, name, "123")
            for name in ["owner", "editor", "alice", "bob", "carol", "dave"]
        }
        cls.tag_a = cls._add_tag("Branch A")
        cls.tag_b = cls._add_tag("Branch B")
        for name, branches in [
            ("alice", [cls.tag_a]),
            ("bob", [cls.tag_b]),
            ("carol", [cls.tag_a]),
            ("dave", [cls.tag_a, cls.tag_b]),
        ]:
            rv = cls.client.put(
                f"/api/users/{name}/",
                json={"branches": branches},
                headers=cls.headers["owner"],
            )
            assert rv.status_code == 200, rv.json
        # the test app is multi-tree, where the treeless user is a site admin
        # whom the owner may not edit; see test_treeless_user
        with cls.app.app_context():
            set_user_branches(get_guid("nomad"), [cls.tag_a])
        # people of branch A, branch B and without branch
        cls.pa = cls._add_person("Anna", "A", [cls.tag_a], "owner")
        cls.pb = cls._add_person("Boris", "B", [cls.tag_b], "owner")
        cls.pu = cls._add_person("Ulf", "U", [], "owner")

    @classmethod
    def tearDownClass(cls):
        cls.dbman.remove_database(cls.name)

    @classmethod
    def _add_tag(cls, name: str) -> str:
        handle = make_handle()
        rv = cls.client.post(
            "/api/tags/",
            json={"_class": "Tag", "handle": handle, "name": name, "color": "#ff0000"},
            headers=cls.headers["owner"],
        )
        assert rv.status_code == 201, rv.json
        return handle

    @classmethod
    def _add_person(cls, first_name, surname, tags, user) -> str:
        obj = person(first_name, surname, tags)
        rv = cls.client.post("/api/people/", json=obj, headers=cls.headers[user])
        assert rv.status_code == 201, rv.json
        return obj["handle"]

    def _get(self, path: str, user: str = "owner") -> dict:
        rv = self.client.get(f"/api/{path}", headers=self.headers[user])
        self.assertEqual(rv.status_code, 200, rv.json)
        return rv.json

    def _put(self, path: str, obj: dict, user: str):
        return self.client.put(f"/api/{path}", json=obj, headers=self.headers[user])

    def _renamed(self, handle: str, first_name: str) -> dict:
        obj = self._get(f"people/{handle}")
        obj["primary_name"]["first_name"] = first_name
        return obj

    def _tree_user_names(self) -> list:
        """Return the names of the tree's users according to `users_of_tree`."""
        query = user_db.session.query(User.name).filter(users_of_tree(self.tree))
        return sorted(row[0] for row in query.all())

    def _branch_users(self, user: str = "owner") -> Dict[str, list]:
        """Return the user names per branch handle from GET /api/branches/."""
        return {
            branch["handle"]: [u["name"] for u in branch["users"]]
            for branch in self._get("branches/", user)
        }

    # user & branch administration

    def test_user_branches(self):
        """Branches are part of the user details and listed per tree."""
        rv = self.client.get("/api/users/-/", headers=self.headers["alice"])
        self.assertEqual(rv.json["branches"], [self.tag_a])
        rv = self.client.get("/api/users/-/", headers=self.headers["owner"])
        self.assertEqual(rv.json["branches"], [])
        rv = self.client.get("/api/users/", headers=self.headers["owner"])
        self.assertEqual(rv.status_code, 200)
        by_name = {user["name"]: user for user in rv.json}
        self.assertEqual(set(by_name["dave"]["branches"]), {self.tag_a, self.tag_b})
        self.assertEqual(by_name["editor"]["branches"], [])
        rv = self.client.get("/api/users/bob/", headers=self.headers["owner"])
        self.assertEqual(rv.json["branches"], [self.tag_b])
        rv = self.client.get("/api/branches/", headers=self.headers["carol"])
        self.assertEqual(rv.status_code, 200)
        branches = {branch["handle"]: branch for branch in rv.json}
        self.assertEqual(branches[self.tag_a]["name"], "Branch A")
        self.assertEqual(
            [user["name"] for user in branches[self.tag_a]["users"]],
            ["alice", "carol", "dave"],
        )
        self.assertEqual(
            [user["name"] for user in branches[self.tag_b]["users"]], ["bob", "dave"]
        )

    def test_set_branches_permissions(self):
        """Only user managers may assign branches, and only existing tags."""
        rv = self.client.put(
            "/api/users/bob/",
            json={"branches": [self.tag_a]},
            headers=self.headers["editor"],
        )
        self.assertEqual(rv.status_code, 403)
        rv = self.client.put(
            "/api/users/bob/",
            json={"branches": [make_handle()]},
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 422)
        rv = self.client.get("/api/users/bob/", headers=self.headers["owner"])
        self.assertEqual(rv.json["branches"], [self.tag_b])
        # new users can be created with branches
        rv = self.client.post(
            "/api/users/erin/",
            json={
                "email": "erin@example.com",
                "full_name": "Erin",
                "password": "123",
                "role": ROLE_EDITOR,
                "branches": [self.tag_b],
            },
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        rv = self.client.get("/api/users/erin/", headers=self.headers["owner"])
        self.assertEqual(rv.json["branches"], [self.tag_b])
        # deleting a user removes the assignment
        rv = self.client.delete("/api/users/erin/", headers=self.headers["owner"])
        self.assertEqual(rv.status_code, 200)
        rv = self.client.get("/api/branches/", headers=self.headers["owner"])
        branches = {branch["handle"]: branch for branch in rv.json}
        self.assertNotIn("erin", [u["name"] for u in branches[self.tag_b]["users"]])

    def test_bulk_users_branches(self):
        """Users created in bulk (an imported user list) keep their branches."""
        users = [
            {
                "name": "bulk_plain",
                "email": "plain@example.com",
                "full_name": "Bulk Plain",
                "role": ROLE_EDITOR,
                "branches": [],  # unrestricted, as exported
            },
            {
                "name": "bulk_branch",
                "email": "branch@example.com",
                "full_name": "Bulk Branch",
                "role": ROLE_EDITOR,
                "tree": self.tree,
                "branches": [self.tag_b],
            },
        ]
        rv = self.client.post("/api/users/", json=users, headers=self.headers["owner"])
        self.assertEqual(rv.status_code, 201, rv.json)
        rv = self.client.get("/api/users/bulk_branch/", headers=self.headers["owner"])
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertEqual(rv.json["branches"], [self.tag_b])
        rv = self.client.get("/api/users/bulk_plain/", headers=self.headers["owner"])
        self.assertEqual(rv.json["branches"], [])
        rv = self.client.get("/api/branches/", headers=self.headers["owner"])
        branches = {branch["handle"]: branch for branch in rv.json}
        self.assertIn("bulk_branch", [u["name"] for u in branches[self.tag_b]["users"]])
        # an unknown tag fails the whole batch: no user is created
        users = [
            {"name": "bulk_ok", "role": ROLE_EDITOR, "branches": [self.tag_a]},
            {"name": "bulk_bad", "role": ROLE_EDITOR, "branches": [make_handle()]},
        ]
        rv = self.client.post("/api/users/", json=users, headers=self.headers["owner"])
        self.assertEqual(rv.status_code, 422)
        for name in ["bulk_ok", "bulk_bad"]:
            rv = self.client.get(f"/api/users/{name}/", headers=self.headers["owner"])
            self.assertEqual(rv.status_code, 404, name)
        # branches must be a list of handles
        rv = self.client.post(
            "/api/users/",
            json=[{"name": "bulk_bad", "role": ROLE_EDITOR, "branches": self.tag_a}],
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 422)
        # only user managers may assign branches
        rv = self.client.post(
            "/api/users/",
            json=[
                {"name": "bulk_editor", "role": ROLE_EDITOR, "branches": [self.tag_a]}
            ],
            headers=self.headers["editor"],
        )
        self.assertEqual(rv.status_code, 403)
        rv = self.client.get("/api/users/bulk_editor/", headers=self.headers["owner"])
        self.assertEqual(rv.status_code, 404)
        # deleting the users removes their assignments
        for name in ["bulk_plain", "bulk_branch"]:
            rv = self.client.delete(
                f"/api/users/{name}/", headers=self.headers["owner"]
            )
            self.assertEqual(rv.status_code, 200, name)
        rv = self.client.get("/api/branches/", headers=self.headers["owner"])
        branches = {branch["handle"]: branch for branch in rv.json}
        self.assertNotIn(
            "bulk_branch", [u["name"] for u in branches[self.tag_b]["users"]]
        )

    def test_token_branches_claim(self):
        """Tokens of restricted users carry their branches; others have no claim."""

        def claims(token: str) -> dict:
            with self.app.app_context():
                return decode_token(token)

        for name, expected in [
            ("alice", [self.tag_a]),
            ("carol", [self.tag_a]),
            ("dave", sorted([self.tag_a, self.tag_b])),
        ]:
            rv = self.client.post(
                "/api/token/", json={"username": name, "password": "123"}
            )
            self.assertEqual(rv.status_code, 200, rv.json)
            self.assertEqual(claims(rv.json["access_token"])["branches"], expected)
            # a refreshed token too
            rv = self.client.post(
                "/api/token/refresh/",
                headers={"Authorization": f"Bearer {rv.json['refresh_token']}"},
            )
            self.assertEqual(rv.status_code, 200, rv.json)
            self.assertEqual(claims(rv.json["access_token"])["branches"], expected)
        # unrestricted users: owners and editors without branches
        for name in ["owner", "editor"]:
            rv = self.client.post(
                "/api/token/", json={"username": name, "password": "123"}
            )
            self.assertEqual(rv.status_code, 200, rv.json)
            self.assertNotIn("branches", claims(rv.json["access_token"]), name)

    def test_treeless_user(self):
        """Users without a tree ID belong to the tree in single-tree mode only."""
        # the test app is multi-tree: there a treeless user is a site admin,
        # not a member of the tree, whatever branch rows they have
        self.assertEqual(self.app.config["TREE"], TREE_MULTI)
        self.assertNotIn("nomad", self._branch_users()[self.tag_a])
        with self.app.app_context():
            self.assertFalse(belongs_to_tree(None, self.tree))
            self.assertTrue(belongs_to_tree(self.tree, self.tree))
            self.assertNotIn("nomad", self._tree_user_names())
            self.assertIn("alice", self._tree_user_names())
        # single-tree mode: the one tree is the treeless users' tree
        with patch.dict(self.app.config, {"TREE": self.name, "TREE_ID": self.tree}):
            with self.app.app_context():
                for user_tree in [None, "", self.tree]:
                    self.assertTrue(belongs_to_tree(user_tree, self.tree), user_tree)
                self.assertFalse(belongs_to_tree("other", self.tree))
                self.assertIn("nomad", self._tree_user_names())
            # ... but only within an app context
            self.assertFalse(belongs_to_tree(None, self.tree))
            self.assertEqual(
                self._branch_users()[self.tag_a], ["alice", "carol", "dave", "nomad"]
            )
            rv = self.client.get("/api/users/", headers=self.headers["owner"])
            by_name = {user["name"]: user for user in rv.json}
            self.assertEqual(by_name["nomad"]["branches"], [self.tag_a])
            # the owner manages their branches like anyone's
            rv = self.client.put(
                "/api/users/nomad/",
                json={"branches": [self.tag_a, self.tag_b]},
                headers=self.headers["owner"],
            )
            self.assertEqual(rv.status_code, 200, rv.json)
            self.assertIn("nomad", self._branch_users()[self.tag_b])
            rv = self.client.put(
                "/api/users/nomad/",
                json={"branches": [self.tag_a]},
                headers=self.headers["owner"],
            )
            self.assertEqual(rv.status_code, 200, rv.json)
            self.assertNotIn("nomad", self._branch_users()[self.tag_b])
            # they log in to the tree and are restricted to their branch
            rv = self.client.post(
                "/api/token/", json={"username": "nomad", "password": "123"}
            )
            self.assertEqual(rv.status_code, 200, rv.json)
            with self.app.app_context():
                claims = decode_token(rv.json["access_token"])
            self.assertEqual(claims["tree"], self.tree)
            self.assertEqual(claims["branches"], [self.tag_a])
            headers = {"Authorization": f"Bearer {rv.json['access_token']}"}
            obj = person("New", "N")
            rv = self.client.post("/api/people/", json=obj, headers=headers)
            self.assertEqual(rv.status_code, 201, rv.json)
            self.assertEqual(
                self._get(f"people/{obj['handle']}")["tag_list"], [self.tag_a]
            )
            rv = self.client.put(
                f"/api/people/{self.pb}",
                json=self._renamed(self.pb, "Nomad"),
                headers=headers,
            )
            self.assertEqual(rv.status_code, 403, rv.json)
        # back in multi-tree mode: excluded again
        self.assertNotIn("nomad", self._branch_users()[self.tag_a])

    # creating

    def test_new_objects_get_branch_tag(self):
        """Objects created by a branch user get the branch tag."""
        handle = self._add_person("New", "A", [], "alice")
        self.assertEqual(self._get(f"people/{handle}")["tag_list"], [self.tag_a])
        # also for contributors
        handle = self._add_person("New", "C", [], "carol")
        self.assertEqual(self._get(f"people/{handle}")["tag_list"], [self.tag_a])
        # a user of two branches without a choice gets both
        handle = self._add_person("New", "D", [], "dave")
        self.assertEqual(
            set(self._get(f"people/{handle}")["tag_list"]), {self.tag_a, self.tag_b}
        )
        # ... but an explicit choice is kept
        handle = self._add_person("New", "D", [self.tag_b], "dave")
        self.assertEqual(self._get(f"people/{handle}")["tag_list"], [self.tag_b])
        # unrestricted users get no tag
        handle = self._add_person("New", "E", [], "editor")
        self.assertEqual(self._get(f"people/{handle}")["tag_list"], [])

    def test_objects_endpoint_tags(self):
        """Objects created in bulk get the branch tag too."""
        note = {
            "_class": "Note",
            "handle": make_handle(),
            "text": {"_class": "StyledText", "string": "A note"},
        }
        event = {
            "_class": "Event",
            "handle": make_handle(),
            "type": {"_class": "EventType", "string": "Birth"},
        }
        rv = self.client.post(
            "/api/objects/", json=[note, event], headers=self.headers["alice"]
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        self.assertEqual(self._get(f"notes/{note['handle']}")["tag_list"], [self.tag_a])
        self.assertEqual(
            self._get(f"events/{event['handle']}")["tag_list"], [self.tag_a]
        )

    # editing

    def test_edit_own_object(self):
        """A branch admin may edit objects of their branch."""
        rv = self._put(f"people/{self.pa}", self._renamed(self.pa, "Anya"), "alice")
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertEqual(
            self._get(f"people/{self.pa}")["primary_name"]["first_name"], "Anya"
        )
        # a user of both branches may edit both
        rv = self._put(f"people/{self.pb}", self._renamed(self.pb, "Borya"), "dave")
        self.assertEqual(rv.status_code, 200, rv.json)

    def test_edit_foreign_object(self):
        """Objects of other branches and without branch are off limits."""
        rv = self._put(f"people/{self.pb}", self._renamed(self.pb, "Bob"), "alice")
        self.assertEqual(rv.status_code, 403)
        self.assertIn("another branch", rv.json["error"]["message"])
        rv = self._put(f"people/{self.pu}", self._renamed(self.pu, "Ulrich"), "alice")
        self.assertEqual(rv.status_code, 403)
        # nothing changed
        self.assertNotEqual(
            self._get(f"people/{self.pb}")["primary_name"]["first_name"], "Bob"
        )
        # unrestricted editors and owners may edit everything
        rv = self._put(f"people/{self.pu}", self._renamed(self.pu, "Ulrich"), "editor")
        self.assertEqual(rv.status_code, 200, rv.json)
        rv = self._put(f"people/{self.pb}", self._renamed(self.pb, "Boris"), "owner")
        self.assertEqual(rv.status_code, 200, rv.json)

    def test_cannot_claim_foreign_object(self):
        """Adding one's own tag to a foreign object is a change of that object."""
        obj = self._get(f"people/{self.pu}")
        obj["tag_list"] = [self.tag_a]
        rv = self._put(f"people/{self.pu}", obj, "alice")
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(self._get(f"people/{self.pu}")["tag_list"], [])

    def test_remove_own_tag(self):
        """Removing the branch tag from an own object gives it up."""
        handle = self._add_person("Giveaway", "A", [], "alice")
        obj = self._get(f"people/{handle}")
        obj["tag_list"] = []
        rv = self._put(f"people/{handle}", obj, "alice")
        self.assertEqual(rv.status_code, 200, rv.json)
        rv = self._put(f"people/{handle}", self._renamed(handle, "Gone"), "alice")
        self.assertEqual(rv.status_code, 403)

    def test_raw_transactions(self):
        """The raw transaction endpoint is guarded too."""
        for handle, status in [(self.pa, 200), (self.pb, 403)]:
            # the raw data format is the one returned by the object endpoints
            rv = self._put(f"people/{handle}", self._get(f"people/{handle}"), "owner")
            self.assertEqual(rv.status_code, 200, rv.json)
            old = rv.json[0]["new"]
            new = deepcopy(old)
            new["primary_name"]["first_name"] = "Raw"
            payload = [
                {
                    "type": "update",
                    "handle": handle,
                    "_class": "Person",
                    "old": old,
                    "new": new,
                }
            ]
            rv = self.client.post(
                "/api/transactions/?force=1",
                json=payload,
                headers=self.headers["alice"],
            )
            self.assertEqual(rv.status_code, status, rv.json)
        self.assertEqual(
            self._get(f"people/{self.pa}")["primary_name"]["first_name"], "Raw"
        )
        self.assertNotEqual(
            self._get(f"people/{self.pb}")["primary_name"]["first_name"], "Raw"
        )

    # deleting

    def test_delete(self):
        """Only objects of one's own branch can be deleted."""
        rv = self.client.delete(f"/api/people/{self.pb}", headers=self.headers["alice"])
        self.assertEqual(rv.status_code, 403)
        self.assertIn("deleted", rv.json["error"]["message"])
        handle = self._add_person("Doomed", "A", [], "alice")
        rv = self.client.delete(f"/api/people/{handle}", headers=self.headers["alice"])
        self.assertEqual(rv.status_code, 200, rv.json)
        rv = self.client.get(f"/api/people/{handle}", headers=self.headers["owner"])
        self.assertEqual(rv.status_code, 404)

    def test_delete_own_child_of_foreign_family(self):
        """Deleting an own person drops its reference from a foreign family."""
        child = self._add_person("Child", "A", [], "alice")
        family = {
            "_class": "Family",
            "handle": make_handle(),
            "father_handle": self.pb,
            "child_ref_list": [{"_class": "ChildRef", "ref": child}],
            "tag_list": [self.tag_b],
        }
        rv = self.client.post(
            "/api/families/", json=family, headers=self.headers["owner"]
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        rv = self.client.delete(f"/api/people/{child}", headers=self.headers["alice"])
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertEqual(
            self._get(f"families/{family['handle']}")["child_ref_list"], []
        )

    # tags

    def test_tags(self):
        """Branch users may create tags but not change or delete them."""
        rv = self.client.post(
            "/api/tags/",
            json={"_class": "Tag", "handle": make_handle(), "name": "Todo"},
            headers=self.headers["alice"],
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        tag = self._get(f"tags/{self.tag_a}")
        tag["name"] = "Renamed"
        rv = self._put(f"tags/{self.tag_a}", tag, "alice")
        self.assertEqual(rv.status_code, 403)
        rv = self.client.delete(
            f"/api/tags/{self.tag_b}", headers=self.headers["alice"]
        )
        self.assertEqual(rv.status_code, 403)
        self.assertEqual(self._get(f"tags/{self.tag_a}")["name"], "Branch A")

    # linking across branches

    def test_family_between_branches(self):
        """A marriage between branches links the other branch's person."""
        groom = self._add_person("Groom", "A", [], "alice")
        family = {
            "_class": "Family",
            "handle": make_handle(),
            "father_handle": groom,
            "mother_handle": self.pb,
        }
        rv = self.client.post(
            "/api/families/", json=family, headers=self.headers["alice"]
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        self.assertEqual(
            self._get(f"families/{family['handle']}")["tag_list"], [self.tag_a]
        )
        self.assertIn(family["handle"], self._get(f"people/{self.pb}")["family_list"])
        # the other branch's person is otherwise untouched: a rename is refused
        obj = self._renamed(self.pb, "Bride")
        rv = self._put(f"people/{self.pb}", obj, "alice")
        self.assertEqual(rv.status_code, 403)
        # bob may add his own child to the family of branch A
        child = self._add_person("Child", "B", [], "bob")
        fam = self._get(f"families/{family['handle']}")
        fam["child_ref_list"] = [{"_class": "ChildRef", "ref": child}]
        rv = self._put(f"families/{family['handle']}", fam, "bob")
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertEqual(
            self._get(f"people/{child}")["parent_family_list"], [family["handle"]]
        )
        # ... but not alice's child, nor change anything else
        fam = self._get(f"families/{family['handle']}")
        fam["child_ref_list"].append({"_class": "ChildRef", "ref": self.pa})
        rv = self._put(f"families/{family['handle']}", fam, "bob")
        self.assertEqual(rv.status_code, 403)
        fam = self._get(f"families/{family['handle']}")
        fam["private"] = True
        rv = self._put(f"families/{family['handle']}", fam, "bob")
        self.assertEqual(rv.status_code, 403)


if __name__ == "__main__":
    unittest.main()
