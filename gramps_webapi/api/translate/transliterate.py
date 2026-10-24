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

"""Transliterate Russian names into Latin letters.

Names are spelled, not translated: a translation model turns "Надежда" into
"Hope" and "Пётр" into "Peter". The scheme is the simplified BGN/PCGN one
English texts use for Russian names (Tatyana, Yevgeny, Dmitry, Maria), but "ё"
is "e" as in Russian passports: Petr, Fedor, Tereshkin (the family's choice).
"""

from __future__ import annotations

_LETTERS = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    # Ukrainian letters show up in old records too
    "і": "i",
    "ї": "yi",
    "є": "ye",
    "ґ": "g",
}

_VOWELS = set("аеёиоуыэюяіїє")


def _is_letter(char: str) -> bool:
    return char.lower() in _LETTERS


def _latin(text: str, i: int) -> str:
    """Return the Latin spelling of the lower-case letter text[i]."""
    char = text[i]
    prev = text[i - 1] if i > 0 and _is_letter(text[i - 1]) else ""
    after = text[i + 1 :]
    at_end = not after or not _is_letter(after[0])
    if char == "е" and (not prev or prev in _VOWELS or prev in "ъь"):
        return "ye"  # Yelena, Matveyevich
    if (
        char in "иы"
        and after[:1] == "й"
        and (len(after) == 1 or not _is_letter(after[1]))
    ):
        return "y"  # Dmitry, Yury: the "й" then vanishes
    if char == "й" and prev in ("и", "ы") and at_end:
        return ""
    if char == "я" and prev == "и" and at_end:
        return "a"  # Maria, Natalia
    if char == "ь" and after[:1] == "и":
        return "y"  # Ilyich
    return _LETTERS.get(char, char)


def transliterate(text: str) -> str:
    """Spell the Cyrillic letters of `text` in Latin ones; the rest stays."""
    lower = text.lower()
    out = []
    for i, char in enumerate(text):
        if not _is_letter(char):
            out.append(char)
            continue
        latin = _latin(lower, i)
        if char.isupper() and latin:
            following = text[i + 1 : i + 2]
            preceding = text[i - 1 : i]
            all_caps = following.isupper() or (
                not following.isalpha() and preceding.isupper()
            )
            # SHCHUKIN, not ShCHUKIN
            latin = latin.upper() if all_caps else latin.capitalize()
        out.append(latin)
    return "".join(out)
