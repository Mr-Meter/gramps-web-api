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

"""Tests for the translation of tree content (Russian <-> English).

The translation model is not needed: tests fill the cache themselves or
replace the model by a stub.
"""

import fcntl
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from gramps.cli.clidbman import CLIDbManager
from gramps.gen.dbstate import DbState

from gramps_webapi.api.translate import machine, store
from gramps_webapi.api.translate.content import (
    LABEL,
    NAME,
    PLACE,
    SUMMARY,
    TEXT,
    convert,
    walk,
)
from gramps_webapi.api.translate.transliterate import transliterate
from gramps_webapi.app import create_app
from gramps_webapi.auth import add_user, user_db
from gramps_webapi.auth.const import ROLE_OWNER
from gramps_webapi.const import ENV_CONFIG_FILE, TEST_AUTH_CONFIG


def make_handle() -> str:
    return str(uuid.uuid4())


class TestTransliterate(unittest.TestCase):
    """Russian names in Latin letters."""

    def test_names(self):
        for russian, latin in [
            ("Татьяна", "Tatyana"),
            ("Пётр", "Petr"),
            ("Евдаким", "Yevdakim"),
            ("Терёшкин", "Tereshkin"),
            ("Гальцова", "Galtsova"),
            ("Мария", "Maria"),
            ("Дмитрий", "Dmitry"),
            ("Матвеевич", "Matveyevich"),
            ("Ильич", "Ilyich"),
            ("Хрущёв", "Khrushchev"),
            ("ЩУКИН", "SHCHUKIN"),
            ("Бродов, Евсей", "Brodov, Yevsey"),
            ("А.М. Шорин", "A.M. Shorin"),
            ("John Smith", "John Smith"),
        ]:
            self.assertEqual(transliterate(russian), latin)


def getter(translations):
    """A cache lookup for `convert` from a dict {(mode, source): result}."""
    return lambda mode, source: translations.get((mode, source))


class TestConvert(unittest.TestCase):
    """Rendering strings of each kind for a reader."""

    def test_name(self):
        get = getter({("name", "John"): "Джон", ("name", "Smith"): "Смит"})
        self.assertEqual(convert(NAME, "Пётр", "en", get), "Petr")
        self.assertEqual(convert(NAME, "Пётр", "ru", get), "Пётр")
        self.assertEqual(
            convert(NAME, "Smith, John J. III", "ru", get), "Смит, Джон J. III"
        )
        # not translated yet: stays
        self.assertEqual(convert(NAME, "Baker", "ru", get), "Baker")

    def test_text(self):
        get = getter({("text", "Свадьба"): "Wedding"})
        self.assertEqual(convert(TEXT, " Свадьба ", "en", get), " Wedding ")
        self.assertEqual(convert(TEXT, "Свадьба", "ru", get), "Свадьба")
        self.assertEqual(convert(TEXT, "Похороны", "en", get), "Похороны")
        self.assertEqual(convert(TEXT, "1912", "en", get), "1912")
        # file names are spelled, not translated
        self.assertEqual(
            convert(TEXT, "Бродов_Илья_Львович", "en", get), "Brodov_Ilya_Lvovich"
        )

    def test_place(self):
        get = getter({("place", "Москва"): "Moscow", ("place", "Россия"): "Russia"})
        self.assertEqual(convert(PLACE, "Москва, Россия", "en", get), "Moscow, Russia")
        # the model gave no English name: spelled when shown
        get = getter(
            {("place", "Орёл"): "Орёл", ("place", "Great Falls"): "Great Falls"}
        )
        self.assertEqual(convert(PLACE, "Орёл", "en", get), "Orel")
        self.assertEqual(convert(PLACE, "Great Falls", "ru", get), "Great Falls")

    def test_label_only_into_english(self):
        get = getter({("text", "Прозвище"): "Nickname", ("text", "Birth"): "Рождение"})
        self.assertEqual(convert(LABEL, "Прозвище", "en", get), "Nickname")
        self.assertEqual(convert(LABEL, "Birth", "ru", get), "Birth")

    def test_summary(self):
        get = getter({("text", "Свадьба"): "Wedding"})
        self.assertEqual(
            convert(SUMMARY, "Свадьба - Шорин, Дмитрий", "en", get),
            "Wedding - Shorin, Dmitry",
        )

    def test_kinds_by_context(self):
        kinds = {}

        def visit(kind, value, path):
            kinds[path] = kind
            return value

        profile = {
            "place": {"name": "Москва", "lat": 55.7},
            "repository": {"name": "Архив, Ростов"},
        }
        walk(profile, "Profile", visit)
        # a place name is spelled when not known; a repository is translated
        self.assertEqual(kinds["place", "name"], PLACE)
        self.assertEqual(kinds["repository", "name"], TEXT)
        # HTML code is left alone, other notes are translated
        kinds.clear()
        walk({"type": "Html code", "text": {"string": "<b>Дед</b>"}}, "Note", visit)
        self.assertNotIn(("text",), kinds)
        walk({"type": "General", "text": {"string": "Дед"}}, "Note", visit)
        self.assertIn(("text",), kinds)


class TestMachineHelpers(unittest.TestCase):
    """Pre- and post-processing around the model."""

    def test_split_keeps_glue(self):
        text = "Родился в г. Москва. Потом А.М. Шорин уехал!\nВторая строка"
        pieces = machine._split(text)
        self.assertEqual("".join(pieces), text)
        self.assertEqual(
            pieces[::2],
            ["Родился в г. Москва.", "Потом А.М. Шорин уехал!", "Вторая строка"],
        )

    def test_tidy(self):
        self.assertEqual(machine._tidy("Прозвище", "nickname.", "text"), "Nickname")
        self.assertEqual(
            machine._tidy("Кирьят-Гат", "kiryat-gath", "place"), "Kiryat-Gath"
        )
        self.assertEqual(machine._tidy("Он умер.", "He died.", "text"), "He died.")

    def test_long_sentences_are_cut(self):
        sentence = ", ".join(f"сын Иван родился в {1900 + i} году" for i in range(40))
        pieces = machine._split(sentence + ".")
        self.assertEqual("".join(pieces), sentence + ".")
        self.assertGreater(len(pieces), 3)
        self.assertTrue(all(len(p) <= machine._MAX_PIECE + 40 for p in pieces[::2]))

    def test_lock_is_not_waited_for_in_the_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "translations.sqlite")
            keys = [("text", "en", "Свадьба")]
            with open(f"{path}.lock", "a", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with patch.object(machine, "available", return_value=True):
                    self.assertIsNone(
                        machine.translate_missing(path, "", keys, blocking=False)
                    )

    def test_spelled(self):
        self.assertTrue(machine._spelled("Москва", "Moscow"))
        self.assertTrue(machine._spelled("Haifa", "Хайфа"))
        self.assertTrue(machine._spelled("Хайфа", "haifa"))
        self.assertFalse(machine._spelled("Красная Горка", "Red Hill"))
        self.assertFalse(machine._spelled("Сокол", "Falcon"))
        self.assertFalse(machine._spelled("Little Rock", "Маленький рок"))
        self.assertFalse(machine._spelled("Hope", "Надежда"))


class TestStore(unittest.TestCase):
    """The SQLite cache of translations."""

    def test_claim_lookup_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "translations.sqlite")
            keys = [("text", "en", "Свадьба"), ("place", "en", "Москва")]
            self.assertEqual(store.claim(path, keys), keys)
            # requested lately: nobody claims them again
            self.assertEqual(store.claim(path, keys), [])
            results, latest = store.lookup(path, keys)
            self.assertEqual(results, {})
            self.assertGreater(latest, 0)
            store.save(path, {keys[0]: "Wedding"})
            results, _ = store.lookup(path, keys)
            self.assertEqual(results, {keys[0]: "Wedding"})
            # withdrawn requests can be made again
            store.release(path, keys)
            self.assertEqual(store.claim(path, keys), keys[1:])
            # the file deleted to drop the cache: made anew, table included
            os.remove(path)
            self.assertEqual(store.claim(path, keys), keys)


class TestTranslationApi(unittest.TestCase):
    """Translated responses, and originals kept on saving."""

    @classmethod
    def setUpClass(cls):
        cls.name = "Test Translation"
        cls.dbman = CLIDbManager(DbState())
        dbpath, _ = cls.dbman.create_new_db_cli(cls.name, dbid="sqlite")
        cls.tree = os.path.basename(dbpath)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "translations.sqlite")
        with patch.dict("os.environ", {ENV_CONFIG_FILE: TEST_AUTH_CONFIG}):
            cls.app = create_app(
                config={
                    "SUCCESSION_CHECK_INTERVAL": 0,
                    "RATELIMIT_ENABLED": False,
                    "CONTENT_TRANSLATION_DB": cls.db_path,
                    "CONTENT_TRANSLATION_WAIT": 0,
                },
                config_from_env=False,
            )
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        with cls.app.app_context():
            user_db.create_all()
            add_user(name="owner", password="123", role=ROLE_OWNER, tree=cls.tree)
        rv = cls.client.post(
            "/api/token/", json={"username": "owner", "password": "123"}
        )
        cls.headers = {"Authorization": f"Bearer {rv.json['access_token']}"}

    @classmethod
    def tearDownClass(cls):
        cls.dbman.remove_database(cls.name)
        cls.tmp.cleanup()

    def post(self, endpoint, obj):
        rv = self.client.post(f"/api/{endpoint}/", json=obj, headers=self.headers)
        self.assertEqual(rv.status_code, 201, rv.json)
        return obj["handle"]

    def get(self, url):
        rv = self.client.get(url, headers=self.headers)
        self.assertEqual(rv.status_code, 200, rv.json)
        return rv

    def put_back(self, endpoint, obj, cls):
        """Save an object the way the frontend does after an edit."""
        obj = {
            k: v
            for k, v in obj.items()
            if k not in ("extended", "profile", "backlinks", "formatted")
        }
        rv = self.client.put(
            f"/api/{endpoint}/{obj['handle']}",
            json={"_class": cls, **obj},
            headers=self.headers,
        )
        self.assertEqual(rv.status_code, 200, rv.json)

    def make_person(self):
        place = self.post(
            "places",
            {
                "_class": "Place",
                "handle": make_handle(),
                "name": {"_class": "PlaceName", "value": "Москва"},
                "place_type": "City",
            },
        )
        event = self.post(
            "events",
            {
                "_class": "Event",
                "handle": make_handle(),
                "type": "Birth",
                "description": "Национальность: Еврей",
                "place": place,
            },
        )
        return self.post(
            "people",
            {
                "_class": "Person",
                "handle": make_handle(),
                "gender": 1,
                "primary_name": {
                    "_class": "Name",
                    "first_name": "Евдаким",
                    "call": "Николай",
                    "type": "Прозвище",
                    "surname_list": [{"_class": "Surname", "surname": "Терёшкин"}],
                },
                "event_ref_list": [
                    {"_class": "EventRef", "ref": event, "role": "Primary"}
                ],
                "birth_ref_index": 0,
            },
        )

    def test_people_translated_and_kept(self):
        store.save(
            self.db_path,
            {
                ("text", "en", "Национальность: Еврей"): "Nationality: Jewish",
                ("place", "en", "Москва"): "Moscow",
                ("text", "en", "Прозвище"): "Nickname",
            },
        )
        handle = self.make_person()
        original = self.get(f"/api/people/{handle}")
        self.assertEqual(original.json["primary_name"]["first_name"], "Евдаким")
        russian = self.get(f"/api/people/{handle}?locale=ru&profile=all")
        self.assertEqual(russian.json["profile"]["name_given"], "Евдаким")

        rv = self.get(f"/api/people/{handle}?locale=en&profile=all&extend=all")
        person = rv.json
        self.assertNotEqual(rv.headers["ETag"], russian.headers["ETag"])
        name = person["primary_name"]
        self.assertEqual(name["first_name"], "Yevdakim")
        self.assertEqual(name["call"], "Nikolay")
        self.assertEqual(name["type"], "Nickname")
        self.assertEqual(name["surname_list"][0]["surname"], "Tereshkin")
        self.assertEqual(person["profile"]["name_display"], "Tereshkin, Yevdakim")
        self.assertEqual(person["profile"]["birth"]["place"], "Moscow")
        self.assertEqual(
            person["extended"]["events"][0]["description"], "Nationality: Jewish"
        )

        # an English reader changes the call name and saves
        name["call"] = "Kolya"
        self.put_back("people", person, "Person")
        name = self.get(f"/api/people/{handle}").json["primary_name"]
        self.assertEqual(name["first_name"], "Евдаким")
        self.assertEqual(name["call"], "Kolya")
        self.assertEqual(name["type"], "Прозвище")
        self.assertEqual(name["surname_list"][0]["surname"], "Терёшкин")

        event = self.get(f"/api/events/{person['event_ref_list'][0]['ref']}?locale=en")
        self.put_back("events", event.json, "Event")
        event = self.get(f"/api/events/{event.json['handle']}")
        self.assertEqual(event.json["description"], "Национальность: Еврей")

    def test_missing_translations_are_made(self):
        def fake_model(db_path, models_dir, keys, blocking=True):
            store.save(db_path, {key: f"[{key[1]}] {key[2]}" for key in keys})
            return len(keys)

        with (
            patch.object(machine, "available", return_value=True),
            patch(
                "gramps_webapi.api.tasks.translate_missing", side_effect=fake_model
            ) as model,
        ):
            handle = self.post(
                "notes",
                {
                    "_class": "Note",
                    "handle": make_handle(),
                    "text": {
                        "_class": "StyledText",
                        "string": "Дед работал на заводе.",
                        "tags": [
                            {
                                "_class": "StyledTextTag",
                                "name": {"_class": "StyledTextTagType", "value": 0},
                                "ranges": [[0, 3]],
                            }
                        ],
                    },
                },
            )
            # queued right after saving, before anyone asked
            model.assert_called()
            results, _ = store.lookup(
                self.db_path, [("text", "en", "Дед работал на заводе.")]
            )
            self.assertEqual(len(results), 1)
            note = self.get(f"/api/notes/{handle}?locale=en&formats=html").json
            # objects created together, as the "new person" form does
            rv = self.client.post(
                "/api/objects/",
                json=[
                    {
                        "_class": "Event",
                        "handle": make_handle(),
                        "type": "Death",
                        "description": "Погиб на фронте",
                    }
                ],
                headers=self.headers,
            )
            self.assertEqual(rv.status_code, 201, rv.json)
            results, _ = store.lookup(self.db_path, [("text", "en", "Погиб на фронте")])
            self.assertEqual(len(results), 1)
        self.assertEqual(note["text"]["string"], "[en] Дед работал на заводе.")
        self.assertEqual(note["text"]["tags"], [])
        self.assertIn("[en] ", note["formatted"]["html"])

        self.put_back("notes", note, "Note")
        text = self.get(f"/api/notes/{handle}").json["text"]
        self.assertEqual(text["string"], "Дед работал на заводе.")
        self.assertEqual(text["tags"][0]["ranges"], [[0, 3]])

    def test_disabled(self):
        handle = self.make_person()
        self.app.config["CONTENT_TRANSLATION"] = False
        try:
            rv = self.get(f"/api/people/{handle}?locale=en")
        finally:
            self.app.config["CONTENT_TRANSLATION"] = True
        self.assertEqual(rv.json["primary_name"]["first_name"], "Евдаким")

    def person_with_names(self, *names):
        """A person with these (first name, surname) pairs as names."""

        def name(first, surname):
            return {
                "_class": "Name",
                "first_name": first,
                "surname_list": [{"_class": "Surname", "surname": surname}],
            }

        return self.post(
            "people",
            {
                "_class": "Person",
                "handle": make_handle(),
                "gender": 1,
                "primary_name": name(*names[0]),
                "alternate_names": [name(*n) for n in names[1:]],
            },
        )

    def names_of(self, handle):
        person = self.get(f"/api/people/{handle}").json
        return [
            (n["first_name"], n["surname_list"][0]["surname"])
            for n in [person["primary_name"]] + person["alternate_names"]
        ]

    def test_originals_of_other_fields_stay(self):
        names = [("Пётр", "Терёшкин"), ("Petr", "Tereshkin"), ("Петр", "Терешкин")]
        handle = self.person_with_names(*names)
        person = self.get(f"/api/people/{handle}?locale=en").json
        self.assertEqual(person["primary_name"]["first_name"], "Petr")
        self.put_back("people", person, "Person")
        self.assertEqual(self.names_of(handle), names)

    def test_russian_reader_keeps_originals(self):
        store.save(self.db_path, {("name", "ru", "Ivan"): "Иван"})
        names = [("Иван", "Петров"), ("Ivan", "Petrov")]
        handle = self.person_with_names(*names)
        person = self.get(f"/api/people/{handle}?locale=ru").json
        self.assertEqual(person["alternate_names"][0]["first_name"], "Иван")
        self.put_back("people", person, "Person")
        self.assertEqual(self.names_of(handle), names)

    def test_moved_items_are_restored(self):
        names = [("Евдаким", "Терёшкин"), ("Коля", "Терёшкин"), ("Николай", "Терёшкин")]
        handle = self.person_with_names(*names)
        person = self.get(f"/api/people/{handle}?locale=en").json
        # the English reader removes an alternate name; the next one moves up
        del person["alternate_names"][0]
        self.put_back("people", person, "Person")
        self.assertEqual(self.names_of(handle), [names[0], names[2]])

    def test_attribute_values_are_data_for_russian_readers(self):
        store.save(self.db_path, {("text", "ru", "Open"): "Открыть"})
        handle = self.post(
            "sources",
            {
                "_class": "Source",
                "handle": make_handle(),
                "title": "Задача",
                "attribute_list": [
                    {"_class": "SrcAttribute", "type": "Status", "value": "Open"}
                ],
            },
        )
        source = self.get(f"/api/sources/{handle}?locale=ru").json
        self.assertEqual(source["attribute_list"][0]["value"], "Open")

    def test_extended_source(self):
        store.save(
            self.db_path, {("text", "en", "Метрическая книга"): "Parish register"}
        )
        source = self.post(
            "sources",
            {"_class": "Source", "handle": make_handle(), "title": "Метрическая книга"},
        )
        citation = self.post(
            "citations",
            {"_class": "Citation", "handle": make_handle(), "source_handle": source},
        )
        rv = self.get(f"/api/citations/{citation}?locale=en&extend=all")
        self.assertEqual(rv.json["extended"]["source"]["title"], "Parish register")

    def test_names_survive_a_failing_queue(self):
        handle = self.person_with_names(("Фёдор", "Ковалёв"))
        with (
            patch.object(machine, "available", return_value=True),
            patch(
                "gramps_webapi.api.tasks.translate_missing",
                side_effect=RuntimeError("queue down"),
            ),
        ):
            rv = self.get(f"/api/people/{handle}?locale=en&profile=self")
        self.assertEqual(rv.json["profile"]["name_display"], "Kovalev, Fedor")
        # withdrawn, so the next request asks again
        key = ("name", "ru", "Fedor")
        self.assertEqual(store.claim(self.db_path, [key]), [key])
