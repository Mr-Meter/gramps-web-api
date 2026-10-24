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

"""Which strings of Gramps objects are translated, and how.

The source language of a string is told by its script: Cyrillic means
Russian, Latin letters without Cyrillic mean English. A string is translated
when the reader's language differs from it.

Machine translations are looked up through a `get(mode, source)` callback
returning the translation or None, so the same code renders a response,
collects the strings still missing and recognises translations sent back.
Formatted note HTML is not translated piece by piece: it is made anew from
the translated text (see `translate._render`).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from gramps.gen.lib import NoteType

from .transliterate import transliterate

LANGUAGES = ("en", "ru")

# kinds of strings
NAME = "name"  # personal names: spelled in the other script
TEXT = "text"  # free text: machine translation
PLACE = "place"  # place titles: each comma-separated part on its own
# custom type names and attribute values: only into English (see `convert`)
LABEL = "label"
SUMMARY = "summary"  # "<event type> - <person name>"
STYLED = "styled"  # StyledText of a note: string plus formatting ranges
KINDS = {NAME, TEXT, PLACE, LABEL, SUMMARY, STYLED}

# modes of the machine translation cache
MODE_TEXT = "text"
MODE_PLACE = "place"
MODE_NAME = "name"  # a single Latin word of a name, into Russian

Getter = Callable[[str, str], Optional[str]]

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-z]")
_LATIN_LETTER = r"A-Za-zÀ-ÖØ-öø-ɏḀ-ỿ"  # with the accented ones: Zieliński
_LATIN_WORD = re.compile(rf"[{_LATIN_LETTER}]+(?:['’-][{_LATIN_LETTER}]+)*")
_ROMAN = re.compile(r"[IVXLCDM]+")
# file names, codes, links: no spaces, but an underscore, slash or "@"
_IDENTIFIER = re.compile(r"\S*[_/\\@]\S*")
_HTML_NOTE = NoteType(NoteType.HTML_CODE).xml_str()


def source_language(text: str) -> Optional[str]:
    """Tell the language of a string by its script."""
    if _CYRILLIC.search(text):
        return "ru"
    if _LATIN.search(text):
        return "en"
    return None


def _convert_text(value: str, target: str, get: Getter, mode: str) -> str:
    """Translate free text, keeping the surrounding whitespace."""
    core = value.strip()
    if not core or source_language(core) in (None, target):
        return value
    if _IDENTIFIER.fullmatch(core):
        return transliterate(value) if target == "en" else value
    result = get(mode, core)
    if result is None:
        return value
    if result == core and mode == MODE_PLACE and target == "en":
        # no English name: spelled now, so the cache is not tied to the
        # transliteration rules
        result = transliterate(core)
    start = value.index(core)
    return value[:start] + result + value[start + len(core) :]


def _convert_name(value: str, target: str, get: Getter) -> str:
    """Spell a name for readers of `target`."""
    if target == "en":
        return transliterate(value) if _CYRILLIC.search(value) else value

    def word(match: re.Match) -> str:
        source = match.group(0)
        if len(source) == 1 or _ROMAN.fullmatch(source):
            return source  # initials and generation numbers stay
        result = get(MODE_NAME, source)
        return source if result is None else result

    return _LATIN_WORD.sub(word, value)


def convert(kind: str, value: Any, target: str, get: Getter) -> Any:
    """Return `value`, a string of the given kind, for readers of `target`."""
    if not isinstance(value, str) or not value.strip():
        return value
    if kind == NAME:
        return _convert_name(value, target, get)
    if kind == PLACE:
        return ", ".join(
            _convert_text(part, target, get, MODE_PLACE) for part in value.split(", ")
        )
    if kind == LABEL:
        # Russian readers keep Latin ones: these may be standard Gramps types
        # without a translation, or values the frontend reads as data (task
        # status "Open", Y-DNA SNPs), which it compares with its own strings
        return _convert_text(value, target, get, MODE_TEXT) if target == "en" else value
    if kind == SUMMARY:
        head, sep, tail = value.partition(" - ")
        if not sep:
            return convert(LABEL, value, target, get)
        return (
            convert(LABEL, head, target, get) + sep + convert(NAME, tail, target, get)
        )
    return _convert_text(value, target, get, MODE_TEXT)


# Translatable fields of each object type in API responses (types are plain
# strings there, no `_class` keys) and in database rows (types are dicts with
# a "string"). A value is either a kind of string or the type of a nested
# object or list of objects.
_ADDRESS = dict.fromkeys(
    ("street", "locality", "city", "county", "state", "country"), PLACE
)
SCHEMA: dict[str, dict[str, str]] = {
    "Person": {
        "primary_name": "Name",
        "alternate_names": "Name",
        "event_ref_list": "EventRef",
        "attribute_list": "Attribute",
        "address_list": "Address",
        "urls": "Url",
        "person_ref_list": "PersonRef",
    },
    "Name": {
        "first_name": NAME,
        "call": NAME,
        "nick": NAME,
        "famnick": NAME,
        "suffix": NAME,
        "title": TEXT,
        "type": LABEL,
        "surname_list": "Surname",
    },
    "Surname": {"surname": NAME, "prefix": NAME},
    "EventRef": {"role": LABEL, "attribute_list": "Attribute"},
    "Family": {
        "type": LABEL,
        "event_ref_list": "EventRef",
        "attribute_list": "Attribute",
    },
    "Event": {"description": TEXT, "type": LABEL, "attribute_list": "Attribute"},
    "Place": {
        "title": PLACE,
        "name": "PlaceName",
        "alt_names": "PlaceName",
        "alt_loc": "Address",
        "urls": "Url",
        "place_type": LABEL,
    },
    "PlaceName": {"value": PLACE},
    "Media": {"desc": TEXT, "attribute_list": "Attribute"},
    "Note": {"text": STYLED, "type": LABEL},
    "Citation": {"page": TEXT, "attribute_list": "Attribute"},
    "Source": {
        "title": TEXT,
        "author": NAME,
        "pubinfo": TEXT,
        "abbrev": TEXT,
        "attribute_list": "Attribute",
    },
    "Repository": {
        "name": TEXT,
        "type": LABEL,
        "address_list": "Address",
        "urls": "Url",
    },
    "Attribute": {"type": LABEL, "value": LABEL},
    "Address": _ADDRESS,
    "Url": {"desc": TEXT},
    "PersonRef": {"rel": TEXT},
    # computed parts of a response next to the object itself
    "Extended": {
        "people": "Person",
        "children": "Person",
        "families": "Family",
        "events": "Event",
        "places": "Place",
        "media": "Media",
        "notes": "Note",
        "citations": "Citation",
        "sources": "Source",
        "repositories": "Repository",
        "source": "Source",
        "place": "Place",
        "father": "Person",
        "mother": "Person",
        "parent_families": "Family",
        "primary_parent_family": "Family",
    },
    # profiles, timelines: computed dicts of any depth; every nested dict
    # is read with this same table
    "Profile": {
        "name_given": NAME,
        "name_surname": NAME,
        "name_patronymic": NAME,
        "name_suffix": NAME,
        "name_display": NAME,
        "family_surname": NAME,
        "author": NAME,
        "place": PLACE,
        "place_name": PLACE,
        "name": PLACE,
        "alternate_names": PLACE,
        "value": PLACE,
        "description": TEXT,
        "page": TEXT,
        "title": TEXT,
        "pubinfo": TEXT,
        "type": LABEL,
        "role": LABEL,
        "relationship": LABEL,
        "label": LABEL,
        "summary": SUMMARY,
    },
}
_COMMON = {"profile": "Profile", "extended": "Extended"}

Path = tuple  # keys and list indices leading to a value
Visitor = Callable[[str, Any, Path], Any]


def _is_html_note(note: dict) -> bool:
    """Tell a note whose text is HTML code: translating it would break it."""
    typ = note.get("type")
    if isinstance(typ, dict):  # database row
        return typ.get("value") == NoteType.HTML_CODE or typ.get("string") == _HTML_NOTE
    return typ == _HTML_NOTE


def _field_kind(typ: str, data: dict, key: str) -> Optional[str]:
    spec = SCHEMA.get(typ, {}).get(key) or _COMMON.get(key)
    if typ == "Profile" and key == "name" and "lat" not in data:
        return TEXT  # a repository, not a place
    if typ == "Note" and key == "text" and _is_html_note(data):
        return None
    return spec


def walk(data: Any, typ: str, visit: Visitor, path: Path = ()) -> None:
    """Replace, in place, the translatable strings of an object of type `typ`.

    `visit(kind, value, path)` returns the new value. It gets strings, except
    for STYLED, where it gets the StyledText dict.
    """
    if isinstance(data, list):
        for i, item in enumerate(data):
            walk(item, typ, visit, path + (i,))
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        spec = _field_kind(typ, data, key)
        here = path + (key,)
        if spec is None:
            if typ == "Profile":
                walk(value, typ, visit, here)
        elif spec not in KINDS:
            walk(value, spec, visit, here)
        elif isinstance(value, str):
            data[key] = visit(spec, value, here)
        elif isinstance(value, dict) and isinstance(value.get("string"), str):
            if spec == STYLED:
                data[key] = visit(spec, value, here)
            else:  # a type in a database row
                value["string"] = visit(spec, value["string"], here)
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            data[key] = [visit(spec, v, here + (i,)) for i, v in enumerate(value)]
        elif typ == "Profile":
            walk(value, typ, visit, here)
