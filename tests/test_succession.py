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

"""Tests for succession plans."""

import os
import time
import unittest
import uuid
from typing import Dict
from unittest.mock import patch

from gramps.cli.clidbman import CLIDbManager
from gramps.gen.dbstate import DbState

from gramps_webapi.app import create_app
from gramps_webapi.auth import add_user, user_db
from gramps_webapi.auth.const import (
    ROLE_DISABLED,
    ROLE_EDITOR,
    ROLE_GUEST,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from gramps_webapi.const import ENV_CONFIG_FILE, TEST_AUTH_CONFIG, TREE_MULTI


def get_headers(client, user: str, password: str) -> Dict[str, str]:
    """Get the auth headers for a specific user."""
    rv = client.post("/api/token/", json={"username": user, "password": password})
    assert rv.status_code == 200, rv.json
    return {"Authorization": "Bearer {}".format(rv.json["access_token"])}


def make_handle() -> str:
    """Make a new valid handle."""
    return str(uuid.uuid4())


class TestSuccession(unittest.TestCase):
    """Roles are handed over after a recorded, confirmed death."""

    @classmethod
    def setUpClass(cls):
        cls.name = "Test Succession"
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
                ("owner", ROLE_OWNER),  # stays
                ("boss", ROLE_OWNER),  # dies
                ("heir", ROLE_EDITOR),
                ("ed1", ROLE_EDITOR),
                ("ed2", ROLE_EDITOR),
                ("member", ROLE_MEMBER),
                ("val", ROLE_EDITOR),  # validation tests only
                ("viewer", ROLE_MEMBER),  # validation tests only
            ]:
                add_user(
                    name=name,
                    password="123",
                    role=role,
                    tree=cls.tree,
                    email=f"{name}@example.com",
                )
            # for the candidate and person summary tests; without e-mail, so
            # the recipient lists checked above stay as they are
            for name, role in [
                ("gone", ROLE_DISABLED),
                ("pers", ROLE_EDITOR),
                ("guest", ROLE_GUEST),  # may not view private records
                ("legacy", ROLE_EDITOR),  # treeless tests: dies, root succeeds
                ("lone", ROLE_EDITOR),  # treeless tests: dies without successor
                ("heir2", ROLE_EDITOR),  # treeless tests: succeeds root
            ]:
                add_user(name=name, password="123", role=role, tree=cls.tree)
            # created with `gramps_webapi user add` without --tree: no tree ID,
            # which makes them a site admin in multi-tree mode (the test app)
            # and a member of the one tree in single-tree mode
            add_user(
                name="root", password="123", role=ROLE_OWNER, email="root@example.com"
            )
        cls.headers = {
            name: get_headers(cls.client, name, "123")
            for name in [
                "owner",
                "boss",
                "heir",
                "ed1",
                "ed2",
                "member",
                "val",
                "viewer",
                "pers",
                "guest",
                "legacy",
                "lone",
                "heir2",
            ]
        }

    @classmethod
    def tearDownClass(cls):
        cls.dbman.remove_database(cls.name)

    def setUp(self):
        # e-mail is not configured in tests; see what would be sent
        self._email_patch = patch("gramps_webapi.api.util.send_email")
        self.send_email = self._email_patch.start()

    def tearDown(self):
        self._email_patch.stop()

    # helpers

    def _add_person(self, first_name: str) -> str:
        handle = make_handle()
        rv = self.client.post(
            "/api/people/",
            json={
                "_class": "Person",
                "handle": handle,
                "gender": 1,
                "primary_name": {
                    "_class": "Name",
                    "first_name": first_name,
                    "surname_list": [{"_class": "Surname", "surname": "Test"}],
                },
            },
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        return handle

    def _put_person(self, handle: str, changes: dict) -> None:
        rv = self.client.get(f"/api/people/{handle}", headers=self.headers["owner"])
        obj = {**rv.json, **changes}
        rv = self.client.put(
            f"/api/people/{handle}", json=obj, headers=self.headers["owner"]
        )
        self.assertEqual(rv.status_code, 200, rv.json)

    def _record_death(self, handle: str) -> None:
        event = make_handle()
        rv = self.client.post(
            "/api/events/",
            json={
                "_class": "Event",
                "handle": event,
                "type": "Death",  # a plain string names a built-in type
            },
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        self._put_person(
            handle,
            {
                "event_ref_list": [
                    {
                        "_class": "EventRef",
                        "ref": event,
                        "role": {"_class": "EventRoleType", "string": "Primary"},
                    }
                ],
                "death_ref_index": 0,
            },
        )

    def _remove_death(self, handle: str) -> None:
        self._put_person(handle, {"event_ref_list": [], "death_ref_index": -1})

    def _plans(self, user: str) -> list:
        rv = self.client.get("/api/succession/", headers=self.headers[user])
        self.assertEqual(rv.status_code, 200, rv.json)
        return rv.json

    def _plan(self, plan_id: int, user: str) -> dict:
        rv = self.client.get(f"/api/succession/{plan_id}/", headers=self.headers[user])
        self.assertEqual(rv.status_code, 200, rv.json)
        return rv.json

    def _subjects(self) -> list:
        return [call.kwargs["subject"] for call in self.send_email.call_args_list]

    # tests

    def test_handover(self):
        """Death recorded, confirmed twice, role handed to the first successor."""
        person = self._add_person("Boss")
        rv = self.client.post(
            "/api/succession/",
            json={
                "person_handle": person,
                "successors": ["heir", "ed1"],
                "required_confirmations": 2,
                "grace_days": 0,
            },
            headers=self.headers["boss"],
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        plan = rv.json
        self.assertEqual(plan["state"], "active")
        self.assertEqual(plan["user"]["name"], "boss")
        self.assertEqual([s["name"] for s in plan["successors"]], ["heir", "ed1"])
        rv = self.client.get(f"/api/people/{person}", headers=self.headers["owner"])
        self.assertEqual(plan["person"]["gramps_id"], rv.json["gramps_id"])
        self.assertIn("Boss", plan["person"]["name"])
        self.assertTrue(plan["is_own"])
        self.assertFalse(plan["can_confirm"])
        plan_id = plan["id"]
        # one plan per user
        rv = self.client.post("/api/succession/", json={}, headers=self.headers["boss"])
        self.assertEqual(rv.status_code, 409)
        # who sees it: not a member, but successors and confirmers
        self.assertEqual(self._plans("member"), [])
        rv = self.client.get(
            f"/api/succession/{plan_id}/", headers=self.headers["member"]
        )
        self.assertEqual(rv.status_code, 403)
        # (other tests' plans may be listed too)
        self.assertIn(plan_id, [p["id"] for p in self._plans("heir")])
        self.assertIn(plan_id, [p["id"] for p in self._plans("ed2")])
        self.assertIn(plan_id, [p["id"] for p in self._plans("owner")])
        # only the user and user managers may change it
        rv = self.client.put(
            f"/api/succession/{plan_id}/",
            json={"grace_days": 1},
            headers=self.headers["ed1"],
        )
        self.assertEqual(rv.status_code, 403)
        rv = self.client.put(
            f"/api/succession/{plan_id}/",
            json={"grace_days": 0},
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 200, rv.json)
        # nobody may confirm yet
        rv = self.client.post(
            f"/api/succession/{plan_id}/confirm/", headers=self.headers["ed1"]
        )
        self.assertEqual(rv.status_code, 403)

        # the death is recorded: noticed right after the transaction
        self._record_death(person)
        plan = self._plan(plan_id, "owner")
        self.assertEqual(plan["state"], "pending")
        self.assertIsNotNone(plan["death_recorded_at"])
        self.assertEqual(len(self._subjects()), 1)
        self.assertIn("death recorded", self._subjects()[0])
        recipients = self.send_email.call_args.kwargs["to"]
        self.assertEqual(
            recipients,
            sorted(
                [
                    "boss@example.com",
                    "heir@example.com",
                    "ed1@example.com",
                    "ed2@example.com",
                    "owner@example.com",
                    "val@example.com",
                ]
            ),
        )
        # confirmations: not by the user, not by members, once per editor
        for user in ["boss", "member"]:
            rv = self.client.post(
                f"/api/succession/{plan_id}/confirm/", headers=self.headers[user]
            )
            self.assertEqual(rv.status_code, 403, user)
        self.assertTrue(self._plan(plan_id, "ed1")["can_confirm"])
        rv = self.client.post(
            f"/api/succession/{plan_id}/confirm/", headers=self.headers["ed1"]
        )
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertEqual(rv.json["state"], "pending")
        self.assertEqual([c["user"]["name"] for c in rv.json["confirmations"]], ["ed1"])
        self.assertFalse(rv.json["can_confirm"])
        rv = self.client.post(
            f"/api/succession/{plan_id}/confirm/", headers=self.headers["ed1"]
        )
        self.assertEqual(rv.status_code, 403)
        # the second confirmation: confirmed and, with no grace period, executed
        rv = self.client.post(
            f"/api/succession/{plan_id}/confirm/", headers=self.headers["ed2"]
        )
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertEqual(rv.json["state"], "executed")
        self.assertEqual(rv.json["executed_successor"]["name"], "heir")
        self.assertIn("confirmed", self._subjects()[1])
        self.assertIn("took over", self._subjects()[2])
        # the heir is an owner now, the boss's account is disabled
        heir = get_headers(self.client, "heir", "123")
        rv = self.client.get("/api/users/-/", headers=heir)
        self.assertEqual(rv.json["role"], ROLE_OWNER)
        rv = self.client.post(
            "/api/token/", json={"username": "boss", "password": "123"}
        )
        self.assertEqual(rv.status_code, 403)
        # the chain continues: the heir has a plan with the remaining successors
        rv = self.client.get("/api/succession/", headers=heir)
        plans = {p["user"]["name"]: p for p in rv.json if p["state"] != "executed"}
        self.assertEqual(plans["heir"]["state"], "active")
        self.assertEqual([s["name"] for s in plans["heir"]["successors"]], ["ed1"])
        self.assertIsNone(plans["heir"]["person_handle"])
        self.assertIsNone(plans["heir"]["person"])

    def test_cancel_and_veto(self):
        """The user cancelling the plan, or logging in, means they are alive."""
        person = self._add_person("Ed Two")
        rv = self.client.post(
            "/api/succession/",
            json={
                "person_handle": person,
                "successors": ["ed1"],
                "required_confirmations": 1,
                "grace_days": 30,
            },
            headers=self.headers["ed2"],
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        plan_id = rv.json["id"]
        self._record_death(person)
        self.assertEqual(self._plan(plan_id, "ed2")["state"], "pending")
        # cancelled by the user: back to the start, and the recorded death is
        # ignored until the person record changes again
        rv = self.client.post(
            f"/api/succession/{plan_id}/cancel/", headers=self.headers["ed2"]
        )
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertEqual(rv.json["state"], "active")
        self.assertIsNotNone(rv.json["vetoed_at"])
        rv = self.client.post("/api/succession/check/", headers=self.headers["owner"])
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertEqual(rv.json["plans"], [])
        self.assertEqual(self._plan(plan_id, "ed2")["state"], "active")
        # the record changes (timestamps have second resolution): pending again
        time.sleep(1.1)
        self._put_person(person, {"gender": 0})
        self.assertEqual(self._plan(plan_id, "ed2")["state"], "pending")
        # logging in with the password cancels it too
        get_headers(self.client, "ed2", "123")
        self.assertEqual(self._plan(plan_id, "ed2")["state"], "active")
        # a death record that is removed again reverts a pending plan
        time.sleep(1.1)
        self._put_person(person, {"gender": 1})
        self.assertEqual(self._plan(plan_id, "ed2")["state"], "pending")
        self._remove_death(person)
        self.assertEqual(self._plan(plan_id, "ed2")["state"], "active")
        # deleting: only the user or a user manager
        rv = self.client.delete(
            f"/api/succession/{plan_id}/", headers=self.headers["ed1"]
        )
        self.assertEqual(rv.status_code, 403)
        rv = self.client.delete(
            f"/api/succession/{plan_id}/", headers=self.headers["ed2"]
        )
        self.assertEqual(rv.status_code, 200)
        rv = self.client.get(f"/api/succession/{plan_id}/", headers=self.headers["ed2"])
        self.assertEqual(rv.status_code, 404)

    def test_no_successor(self):
        """Without an eligible successor the plan waits and the owners are told."""
        person = self._add_person("Ed One")
        rv = self.client.post(
            "/api/succession/",
            json={
                "user": "ed1",
                "person_handle": person,
                "successors": [],
                "required_confirmations": 0,
                "grace_days": 0,
            },
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        plan_id = rv.json["id"]
        self._record_death(person)
        plan = self._plan(plan_id, "owner")
        self.assertEqual(plan["state"], "confirmed")
        self.assertIn("no successor", self._subjects()[-1])
        recipients = self.send_email.call_args.kwargs["to"]
        self.assertIn("owner@example.com", recipients)
        self.assertNotIn("ed2@example.com", recipients)
        # in multi-tree mode, an owner without a tree ID is a site admin
        self.assertNotIn("root@example.com", recipients)
        # naming a successor completes it at the next check
        rv = self.client.put(
            f"/api/succession/{plan_id}/",
            json={"successors": ["member"]},
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertEqual(rv.json["state"], "executed")
        self.assertEqual(rv.json["executed_successor"]["name"], "member")
        rv = self.client.get("/api/users/member/", headers=self.headers["owner"])
        self.assertEqual(rv.json["role"], ROLE_EDITOR)

    def test_candidates(self):
        """Editors get the tree's other enabled users as successor candidates."""
        rv = self.client.get("/api/succession/candidates/", headers=self.headers["val"])
        self.assertEqual(rv.status_code, 200, rv.json)
        names = [user["name"] for user in rv.json]
        self.assertEqual(names, sorted(names))
        self.assertNotIn("val", names)  # not oneself
        self.assertNotIn("gone", names)  # not disabled accounts
        for name in ["owner", "ed1", "viewer", "guest"]:
            self.assertIn(name, names)
        for user in rv.json:
            self.assertEqual(set(user), {"name", "full_name", "role"})
        # members have nothing to hand over
        rv = self.client.get(
            "/api/succession/candidates/", headers=self.headers["viewer"]
        )
        self.assertEqual(rv.status_code, 403)
        # a user manager planning for someone else gets everyone but them
        rv = self.client.get(
            "/api/succession/candidates/",
            query_string={"user": "heir"},
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 200, rv.json)
        names = [user["name"] for user in rv.json]
        self.assertIn("owner", names)
        self.assertNotIn("heir", names)
        # one's own name changes nothing and needs no further rights
        rv = self.client.get(
            "/api/succession/candidates/",
            query_string={"user": "val"},
            headers=self.headers["val"],
        )
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertNotIn("val", [user["name"] for user in rv.json])
        # ... but naming someone else does
        rv = self.client.get(
            "/api/succession/candidates/",
            query_string={"user": "ed1"},
            headers=self.headers["val"],
        )
        self.assertEqual(rv.status_code, 403)
        # the user must exist in the tree
        rv = self.client.get(
            "/api/succession/candidates/",
            query_string={"user": "nobody"},
            headers=self.headers["owner"],
        )
        self.assertEqual(rv.status_code, 422)

    def test_person_summary(self):
        """Plans summarise the linked person, unless hidden from the viewer."""
        person = self._add_person("Hidden")
        self._put_person(person, {"private": True})
        rv = self.client.post(
            "/api/succession/",
            json={"person_handle": person, "successors": ["guest"]},
            headers=self.headers["pers"],
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        plan_id = rv.json["id"]
        rv_person = self.client.get(
            f"/api/people/{person}", headers=self.headers["owner"]
        )
        self.assertEqual(set(rv.json["person"]), {"gramps_id", "name"})
        self.assertEqual(rv.json["person"]["gramps_id"], rv_person.json["gramps_id"])
        self.assertIn("Hidden", rv.json["person"]["name"])
        # a guest may not view private records: the person is hidden from them
        plan = self._plan(plan_id, "guest")
        self.assertEqual(plan["person_handle"], person)
        self.assertIsNone(plan["person"])
        self.assertIsNotNone(self._plan(plan_id, "pers")["person"])
        # a person deleted from the tree
        rv = self.client.delete(f"/api/people/{person}", headers=self.headers["owner"])
        self.assertEqual(rv.status_code, 200, rv.json)
        plan = self._plan(plan_id, "pers")
        self.assertEqual(plan["person_handle"], person)
        self.assertIsNone(plan["person"])
        # no person at all
        rv = self.client.put(
            f"/api/succession/{plan_id}/",
            json={"person_handle": None},
            headers=self.headers["pers"],
        )
        self.assertEqual(rv.status_code, 200, rv.json)
        self.assertIsNone(rv.json["person_handle"])
        self.assertIsNone(rv.json["person"])

    def test_treeless_users(self):
        """Users without a tree ID belong to the tree in single-tree mode only."""
        # the test app is multi-tree: there a treeless user is a site admin,
        # not a member of the tree
        self.assertEqual(self.app.config["TREE"], TREE_MULTI)
        rv = self.client.get("/api/succession/candidates/", headers=self.headers["val"])
        self.assertNotIn("root", [user["name"] for user in rv.json])
        for body, user in [
            ({"successors": ["root"]}, "legacy"),
            ({"user": "root"}, "owner"),
        ]:
            rv = self.client.post(
                "/api/succession/", json=body, headers=self.headers[user]
            )
            self.assertEqual(rv.status_code, 422, rv.json)
        legacy = self._add_person("Legacy")
        rv = self.client.post(
            "/api/succession/",
            json={
                "person_handle": legacy,
                "successors": [],
                "required_confirmations": 1,
                "grace_days": 0,
            },
            headers=self.headers["legacy"],
        )
        self.assertEqual(rv.status_code, 201, rv.json)
        plan_id = rv.json["id"]
        confirmers = [u["name"] for u in rv.json["eligible_confirmers"]]
        self.assertNotIn("root", confirmers)
        self.assertIn("val", confirmers)

        # single-tree mode: the one tree is the treeless users' tree
        with patch.dict(self.app.config, {"TREE": self.name, "TREE_ID": self.tree}):
            root = get_headers(self.client, "root", "123")
            # a candidate, and a user who can name candidates
            rv = self.client.get(
                "/api/succession/candidates/", headers=self.headers["val"]
            )
            self.assertIn("root", [user["name"] for user in rv.json])
            rv = self.client.get("/api/succession/candidates/", headers=root)
            self.assertEqual(rv.status_code, 200, rv.json)
            names = [user["name"] for user in rv.json]
            self.assertNotIn("root", names)
            self.assertIn("val", names)
            # a successor, and an eligible confirmer
            rv = self.client.put(
                f"/api/succession/{plan_id}/",
                json={"successors": ["root"]},
                headers=self.headers["legacy"],
            )
            self.assertEqual(rv.status_code, 200, rv.json)
            self.assertEqual([s["name"] for s in rv.json["successors"]], ["root"])
            self.assertIn("root", [u["name"] for u in rv.json["eligible_confirmers"]])
            rv = self.client.get(f"/api/succession/{plan_id}/", headers=root)
            self.assertEqual(rv.status_code, 200, rv.json)
            self.assertFalse(rv.json["can_confirm"])
            self._record_death(legacy)
            self.assertIn("death recorded", self._subjects()[-1])
            self.assertIn("root@example.com", self.send_email.call_args.kwargs["to"])
            rv = self.client.get(f"/api/succession/{plan_id}/", headers=root)
            self.assertEqual(rv.json["state"], "pending")
            self.assertTrue(rv.json["can_confirm"])
            rv = self.client.post(f"/api/succession/{plan_id}/confirm/", headers=root)
            self.assertEqual(rv.status_code, 200, rv.json)
            # the treeless successor of a tree-bound user takes over
            self.assertEqual(rv.json["state"], "executed")
            self.assertEqual(rv.json["executed_successor"]["name"], "root")
            self.assertIn("took over", self._subjects()[-1])
            rv = self.client.post(
                "/api/token/", json={"username": "legacy", "password": "123"}
            )
            self.assertEqual(rv.status_code, 403)
            # owners without a tree ID are told when nobody can take over
            lone = self._add_person("Lone")
            rv = self.client.post(
                "/api/succession/",
                json={
                    "person_handle": lone,
                    "successors": [],
                    "required_confirmations": 0,
                    "grace_days": 0,
                },
                headers=self.headers["lone"],
            )
            self.assertEqual(rv.status_code, 201, rv.json)
            self._record_death(lone)
            self.assertIn("no successor", self._subjects()[-1])
            recipients = self.send_email.call_args.kwargs["to"]
            self.assertIn("root@example.com", recipients)
            self.assertIn("owner@example.com", recipients)
            # a plan for a treeless user, whose tree-bound successor takes over
            rv = self.client.get("/api/succession/", headers=root)
            chain = [p for p in rv.json if p["user"]["name"] == "root"]
            self.assertEqual([p["state"] for p in chain], ["active"])
            rv = self.client.delete(
                f"/api/succession/{chain[0]['id']}/", headers=self.headers["owner"]
            )
            self.assertEqual(rv.status_code, 200)
            person = self._add_person("Root")
            rv = self.client.post(
                "/api/succession/",
                json={
                    "user": "root",
                    "person_handle": person,
                    "successors": ["heir2"],
                    "required_confirmations": 0,
                    "grace_days": 0,
                },
                headers=self.headers["owner"],
            )
            self.assertEqual(rv.status_code, 201, rv.json)
            plan_id = rv.json["id"]
            self._record_death(person)
            plan = self._plan(plan_id, "owner")
            self.assertEqual(plan["state"], "executed")
            self.assertEqual(plan["executed_successor"]["name"], "heir2")
            rv = self.client.get("/api/users/heir2/", headers=self.headers["owner"])
            self.assertEqual(rv.json["role"], ROLE_OWNER)
            rv = self.client.post(
                "/api/token/", json={"username": "root", "password": "123"}
            )
            self.assertEqual(rv.status_code, 403)

    def test_validation(self):
        """Successors and people must exist and belong to the tree."""
        rv = self.client.post(
            "/api/succession/",
            json={"successors": ["nobody"]},
            headers=self.headers["val"],
        )
        self.assertEqual(rv.status_code, 422)
        rv = self.client.post(
            "/api/succession/",
            json={"successors": ["val"]},
            headers=self.headers["val"],
        )
        self.assertEqual(rv.status_code, 422)
        rv = self.client.post(
            "/api/succession/",
            json={"person_handle": make_handle()},
            headers=self.headers["val"],
        )
        self.assertEqual(rv.status_code, 422)
        # members cannot create plans, and only user managers for others
        rv = self.client.post(
            "/api/succession/", json={}, headers=self.headers["viewer"]
        )
        self.assertEqual(rv.status_code, 403)
        rv = self.client.post(
            "/api/succession/", json={"user": "ed2"}, headers=self.headers["val"]
        )
        self.assertEqual(rv.status_code, 403)


if __name__ == "__main__":
    unittest.main()
