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

"""Cache of machine translations: a small SQLite file.

Every distinct string is translated once. The web workers only read it; the
background task fills it. A row without a result is a pending request.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from typing import Iterable

# (mode, target language, source string)
Key = tuple[str, str, str]

# a pending request older than this is made again (the worker died, ...)
RETRY_AFTER = 600

_CHUNK = 400  # stay below the SQLite limit of bound parameters


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    # every time, not once per process: the file may have been deleted to
    # drop the cached translations
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS translation ("
        " mode TEXT NOT NULL, lang TEXT NOT NULL, source TEXT NOT NULL,"
        " result TEXT, requested REAL,"
        " PRIMARY KEY (mode, lang, source)) WITHOUT ROWID"
    )
    return con


def _grouped(keys: Iterable[Key]):
    groups = defaultdict(list)
    for mode, lang, source in keys:
        groups[mode, lang].append(source)
    for (mode, lang), sources in groups.items():
        for i in range(0, len(sources), _CHUNK):
            yield mode, lang, sources[i : i + _CHUNK]


def lookup(path: str, keys: Iterable[Key]) -> tuple[dict[Key, str], float]:
    """Return the known translations and the time of the latest pending request."""
    results: dict[Key, str] = {}
    latest_request = 0.0
    if not os.path.exists(path):  # nothing translated yet; reading creates nothing
        return results, latest_request
    with closing(_connect(path)) as con:
        for mode, lang, sources in _grouped(keys):
            rows = con.execute(
                "SELECT source, result, requested FROM translation"
                f" WHERE mode = ? AND lang = ? AND source IN ({','.join('?' * len(sources))})",
                [mode, lang, *sources],
            )
            for source, result, requested in rows:
                if result is not None:
                    results[mode, lang, source] = result
                elif requested:
                    latest_request = max(latest_request, requested)
    return results, latest_request


def claim(path: str, keys: Iterable[Key]) -> list[Key]:
    """Mark keys as requested; return those nobody has requested lately."""
    now = time.time()
    claimed = []
    with closing(_connect(path)) as con:
        for key in keys:
            cursor = con.execute(
                "INSERT INTO translation (mode, lang, source, requested)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (mode, lang, source) DO UPDATE"
                " SET requested = excluded.requested"
                " WHERE result IS NULL AND (requested IS NULL OR requested < ?)",
                [*key, now, now - RETRY_AFTER],
            )
            if cursor.rowcount:
                claimed.append(key)
        con.commit()
    return claimed


def release(path: str, keys: Iterable[Key]) -> None:
    """Withdraw requests that could not be sent, so they are made again."""
    with closing(_connect(path)) as con:
        con.executemany(
            "UPDATE translation SET requested = NULL"
            " WHERE mode = ? AND lang = ? AND source = ? AND result IS NULL",
            list(keys),
        )
        con.commit()


def save(path: str, results: dict[Key, str]) -> None:
    """Store translations."""
    with closing(_connect(path)) as con:
        con.executemany(
            "INSERT INTO translation (mode, lang, source, result) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (mode, lang, source) DO UPDATE SET result = excluded.result",
            [(*key, result) for key, result in results.items()],
        )
        con.commit()
