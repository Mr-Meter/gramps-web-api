"""Tests for splitting a patronymic off a name."""

import unittest

from gramps.gen.lib import Name, NameOriginType, Person, Surname

from gramps_webapi.api.resources.util import (
    display_name_with_patronymic,
    split_patronymic,
)


def _surname(text, origin=None, primary=True, prefix=""):
    surname = Surname()
    surname.set_surname(text)
    surname.set_prefix(prefix)
    surname.set_primary(primary)
    if origin is not None:
        surname.set_origintype(NameOriginType(origin))
    return surname


def _name(*surnames):
    name = Name()
    name.set_first_name("Пётр")
    name.set_surname_list(list(surnames))
    return name


class TestSplitPatronymic(unittest.TestCase):
    def test_surname_and_patronymic(self):
        name = _name(
            _surname("Никулин"),
            _surname("Петрович", NameOriginType.PATRONYMIC, primary=False),
        )
        self.assertEqual(split_patronymic(name), ("Никулин", "Петрович"))

    def test_without_patronymic(self):
        name = _name(_surname("Никулин"), _surname("Fernández", prefix="El"))
        self.assertEqual(split_patronymic(name), ("Никулин El Fernández", ""))

    def test_patronymic_only(self):
        name = _name(_surname("Петрович", NameOriginType.PATRONYMIC, primary=False))
        self.assertEqual(split_patronymic(name), ("", "Петрович"))

    def test_primary_patronymic_stays_surname(self):
        # e.g. Icelandic names, where the patronymic is the family name
        name = _name(_surname("Jónsson", NameOriginType.PATRONYMIC))
        self.assertEqual(split_patronymic(name), ("Jónsson", ""))

    def test_matronymic_stays_surname(self):
        name = _name(
            _surname("Никулин"),
            _surname("Марьина", NameOriginType.MATRONYMIC, primary=False),
        )
        self.assertEqual(split_patronymic(name), ("Никулин Марьина", ""))


class TestDisplayNameWithPatronymic(unittest.TestCase):
    def test_patronymic_after_given_name(self):
        person = Person()
        person.set_primary_name(
            _name(
                _surname("Никулин"),
                _surname("Петрович", NameOriginType.PATRONYMIC, primary=False),
            )
        )
        self.assertEqual(
            display_name_with_patronymic(person), "Никулин, Пётр Петрович"
        )

    def test_without_surname(self):
        person = Person()
        person.set_primary_name(_name(_surname("")))
        self.assertEqual(display_name_with_patronymic(person), "Пётр")
