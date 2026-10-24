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

"""Show the family tree in the reader's language: Russian <-> English.

The frontend sends the interface language as the `locale` query argument of
every request for objects. When it is English, Russian strings of the
response are rendered in English, and the other way around:

- names are transliterated (Tatyana, Petr) at once, in the web process;
  Latin names for Russian readers come from the translation model;
- other texts come from the translation model, through a cache: missing
  strings are queued for the background worker, which loads the model only
  for that batch. The request waits a few seconds for them, then shows the
  originals; the next request shows the translation.

The database is never changed: when an object comes back in a PUT, every
string the client got translated and left alone is put back as it was.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from typing import Any, Iterable, Optional

from flask import Response, current_app, request
from gramps.gen.db.base import DbReadBase
from gramps.gen.lib.json_utils import object_to_dict

from . import machine, store
from .content import LANGUAGES, SCHEMA, STYLED, TEXT, Path, convert, walk
from .store import Key

_LOG = logging.getLogger(__name__)

_OBJECTS = re.compile(
    r"/api/(people|families|events|places|citations|sources|repositories|media|notes)/(?:[^/]+/?)?"
)
_PROFILES = re.compile(
    r"/api/(?:(?:people|families)/[^/]+/timeline|timelines/(?:people|families)/"
    r"|places/coordinates/)"
)
_CLASSES = {
    "people": "Person",
    "families": "Family",
    "events": "Event",
    "places": "Place",
    "citations": "Citation",
    "sources": "Source",
    "repositories": "Repository",
    "media": "Media",
    "notes": "Note",
}
_SEARCH = "Search"
_POLL = 0.25  # seconds between looks for translations while waiting

_ITERATORS = {
    "Person": "iter_people",
    "Family": "iter_families",
    "Event": "iter_events",
    "Place": "iter_places",
    "Citation": "iter_citations",
    "Source": "iter_sources",
    "Repository": "iter_repositories",
    "Media": "iter_media",
    "Note": "iter_notes",
}


def target_language(locale: Optional[str]) -> Optional[str]:
    """Return the language of a locale argument if it is one we translate into."""
    lang = (locale or "")[:2].lower()
    return lang if lang in LANGUAGES else None


def _response_type(path: str) -> Optional[str]:
    """Return the type of the objects a GET request path answers with."""
    if _PROFILES.fullmatch(path):
        return "Profile"
    if path == "/api/search/":
        return _SEARCH
    match = _OBJECTS.fullmatch(path)
    return _CLASSES[match.group(1)] if match else None


def _walk_response(data: Any, typ: str, visit) -> None:
    if typ != _SEARCH:
        walk(data, typ, visit)
        return
    for hit in data if isinstance(data, list) else []:
        if isinstance(hit, dict) and isinstance(hit.get("object_type"), str):
            walk(hit.get("object"), hit["object_type"].capitalize(), visit)


def _collect(data: Any, typ: str, targets: Iterable[str]) -> set[Key]:
    """Return the cache keys of the machine translations `data` needs."""
    keys: set[Key] = set()
    for target in targets:

        def get(mode: str, source: str, target=target) -> None:
            keys.add((mode, target, source))

        def visit(kind: str, value: Any, path: Path, target=target) -> Any:
            if kind == STYLED:
                convert(TEXT, value["string"], target, get)
            else:
                convert(kind, value, target, get)
            return value

        _walk_response(data, typ, visit)
    return keys


def _notes_with_html(data: Any) -> list[tuple[dict, str]]:
    """Find the notes of a response that carry formatted HTML, with their text."""
    found = []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            text, formatted = node.get("text"), node.get("formatted")
            if (
                isinstance(text, dict)
                and isinstance(text.get("string"), str)
                and isinstance(formatted, dict)
                and "html" in formatted
            ):
                found.append((node, text["string"]))
            stack.extend(node.values())
    return found


def _note_html(text: str, note_format: Any) -> str:
    """Return the HTML of a note text without formatting."""
    # pylint: disable=import-outside-toplevel
    from gramps.gen.lib import Note, StyledText

    from ..html import get_note_html

    note = Note()
    note.set_styledtext(StyledText(text))
    note.set_format(note_format if isinstance(note_format, int) else 0)
    return get_note_html(note)


def _render(data: Any, typ: str, target: str, results: dict[Key, str]) -> bool:
    """Translate `data` in place with the known translations; tell if it changed."""
    changed = False
    notes = _notes_with_html(data)

    def get(mode: str, source: str) -> Optional[str]:
        return results.get((mode, target, source))

    def visit(kind: str, value: Any, path: Path) -> Any:
        nonlocal changed
        if kind != STYLED:
            result = convert(kind, value, target, get)
        else:
            string = convert(TEXT, value["string"], target, get)
            # formatting ranges do not fit the translation
            result = (
                value
                if string == value["string"]
                else {
                    **value,
                    "string": string,
                    "tags": [],
                }
            )
        changed = changed or result != value
        return result

    _walk_response(data, typ, visit)
    # formatted HTML is made from the translated text, not translated piece
    # by piece: one translation per note, the one `translate all` prepares
    for note, original in notes:
        if note["text"]["string"] != original:
            note["formatted"]["html"] = _note_html(
                note["text"]["string"], note.get("format")
            )
    return changed


def _settings() -> tuple[str, str]:
    config = current_app.config
    return (
        config["CONTENT_TRANSLATION_DB"],
        config.get("CONTENT_TRANSLATION_MODELS") or "",
    )


def _dispatch(keys: list[Key]) -> None:
    """Have the keys translated: in the background, or right now without a queue."""
    # pylint: disable=import-outside-toplevel
    from ..tasks import run_task, translate_content

    run_task(translate_content, keys=[list(key) for key in keys])


def _fetch(keys: set[Key], wait: bool) -> dict[Key, str]:
    """Return the known translations; request the missing ones."""
    db_path, models_dir = _settings()
    results, _ = store.lookup(db_path, keys)
    missing = [
        key
        for key in keys
        if key not in results and machine.available(models_dir, key[1])
    ]
    if not missing:
        return results
    claimed = store.claim(db_path, missing)
    if claimed:
        try:
            _dispatch(claimed)
        except Exception:  # pylint: disable=broad-except
            # e.g. the queue is down: let the next request try again
            store.release(db_path, claimed)
            _LOG.warning("Could not queue translations", exc_info=True)
            return results
    if not wait:
        return results
    timeout = float(current_app.config.get("CONTENT_TRANSLATION_WAIT") or 0)
    deadline = time.time() + timeout
    while missing:
        found, latest_request = store.lookup(db_path, missing)
        results.update(found)
        missing = [key for key in missing if key not in found]
        # don't wait for requests nobody works on any more
        if not missing or time.time() >= min(deadline, latest_request + timeout):
            break
        time.sleep(_POLL)
    return results


def _translate_get(response: Response) -> None:
    target = target_language(request.args.get("locale"))
    typ = _response_type(request.path)
    if (
        not target
        or not typ
        or response.status_code != 200
        or response.mimetype != "application/json"
        or response.direct_passthrough
    ):
        return
    data = json.loads(response.get_data())
    keys = _collect(data, typ, [target])
    results: dict[Key, str] = {}
    if keys:
        try:
            results = _fetch(keys, wait=True)
        except sqlite3.Error:
            # names are still spelled without the cache
            _LOG.warning("Translation cache unavailable", exc_info=True)
    if not _render(data, typ, target, results):
        return
    body = json.dumps(data, ensure_ascii=False, sort_keys=True)
    response.set_data(body)
    response.set_etag(hashlib.sha256(body.encode("utf-8")).hexdigest())


def _prepare_written(response: Response) -> None:
    """Queue the translations of objects just written, before anyone asks."""
    written = _OBJECTS.fullmatch(request.path) or request.path == "/api/objects/"
    if response.status_code not in (200, 201) or not written:
        return
    if response.mimetype != "application/json":
        return
    keys: set[Key] = set()
    for item in json.loads(response.get_data()) or []:
        # transaction items: {"_class": ..., "new": <database row>, ...}
        if isinstance(item, dict) and item.get("_class") in SCHEMA and item.get("new"):
            keys |= _collect(item["new"], item["_class"], LANGUAGES)
    if keys:
        _fetch(keys, wait=False)


def translate_response(response: Response) -> Response:
    """Flask `after_request` hook: translate the content of object responses."""
    if not current_app.config.get("CONTENT_TRANSLATION"):
        return response
    try:
        if request.method == "GET":
            _translate_get(response)
        elif request.method in ("POST", "PUT"):
            _prepare_written(response)
    except Exception:  # pylint: disable=broad-except
        # never fail a request over a translation
        _LOG.warning("Content translation failed", exc_info=True)
    return response


def _field(path: Path) -> tuple:
    """The field a path leads to, whatever the list positions: a.b[2].c -> a.b.c."""
    return tuple(step for step in path if isinstance(step, str))


def restore_originals(incoming: Any, stored: dict, typ: str) -> None:
    """Put back, in place, the originals of translated strings left unchanged.

    `incoming` is an object sent by a client in the API format, `stored` the
    same object as it is in the database (API format too). A string of
    `incoming` that is how a reader of either language was shown a string of
    the same field in `stored` is replaced by that original: preferably the
    one at the same position, else the only one of that field (items may
    have moved). Originals and edited strings stay.
    """
    if not current_app.config.get("CONTENT_TRANSLATION") or not isinstance(
        incoming, dict
    ):
        return
    # plain JSON values: what is put back must pass the schema validation
    stored = json.loads(json.dumps(stored, default=str))
    keys = _collect(stored, typ, LANGUAGES)
    results: dict[Key, str] = {}
    if keys:
        try:
            results, _ = store.lookup(_settings()[0], keys)
        except sqlite3.Error:
            # without the cache the client was shown no machine translation
            _LOG.warning("Translation cache unavailable", exc_info=True)
    originals: dict[tuple, dict[str, Any]] = {}  # (kind, field) -> {text: value}
    shown: dict[tuple, dict[str, set[str]]] = {}  # (kind, field) -> {shown: texts}
    at_path: dict[tuple, str] = {}  # (kind, path) -> original text

    def getter(target: str):
        return lambda mode, source: results.get((mode, target, source))

    getters = {target: getter(target) for target in LANGUAGES}

    def remember(kind: str, value: Any, path: Path) -> Any:
        text = value["string"] if kind == STYLED else value
        group = (kind, _field(path))
        originals.setdefault(group, {})[text] = value
        at_path[kind, path] = text
        for target, get in getters.items():
            translated = convert(TEXT if kind == STYLED else kind, text, target, get)
            if translated != text:
                shown.setdefault(group, {}).setdefault(translated, set()).add(text)
        return value

    walk(stored, typ, remember)
    if not shown:
        return

    def restore(kind: str, value: Any, path: Path) -> Any:
        text = value["string"] if kind == STYLED else value
        group = (kind, _field(path))
        shown_as = shown.get(group, {}).get(text, set())
        here = at_path.get((kind, path))
        # what this very position had: either kept or shown translated
        if here == text:
            return value
        if here in shown_as:
            return originals[group][here]
        # items moved (one removed, ...): match within the same field
        if text in originals.get(group, {}) or len(shown_as) != 1:
            return value
        return originals[group][next(iter(shown_as))]

    walk(incoming, typ, restore)


def translate_tree(db_handle: DbReadBase) -> int:
    """Translate every string of a tree that is not in the cache yet."""
    keys: set[Key] = set()
    for typ, iterator in _ITERATORS.items():
        for obj in getattr(db_handle, iterator)():
            keys |= _collect(object_to_dict(obj), typ, LANGUAGES)
    db_path, models_dir = _settings()
    return machine.translate_missing(db_path, models_dir, sorted(keys)) or 0
