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

"""Machine translation with local CTranslate2 models.

The models are the Argos Translate packages ru_en and en_ru (OPUS data),
unpacked to <models dir>/ru_en and <models dir>/en_ru, each holding a
model/ directory and sentencepiece.model. A model is loaded only while a
batch of new strings is translated and unloaded right after: about 0.7 GB of
memory for a second or two, nothing the rest of the time.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import time
from contextlib import contextmanager
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Optional

from . import store
from .content import MODE_NAME, MODE_PLACE
from .store import Key
from .transliterate import transliterate

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore

_LOG = logging.getLogger(__name__)

MODELS = {"en": "ru_en", "ru": "en_ru"}  # target language -> model
THREADS = 2  # CPU threads of one translation; a background job, no hurry

# a name alone is often translated as a word ("Baker" -> "пекарь"),
# inside this sentence it is spelled ("Меня зовут Бейкер.")
_NAME_CARRIER = "My name is {}."
_NAME_ANSWER = re.compile(r"Меня зовут\s+(\S+?)\s*\.?")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_SENTENCE_END = re.compile(r"([.!?…]+)(\s+)(?=[\"«(\[]?[0-9A-ZА-ЯЁ])")
_END_PUNCTUATION = tuple('.!?…:;"»)')
# rough sound-alike spelling, see `_spelled`
_SOUNDS = [
    ("shch", "sh"),
    ("dzh", "j"),
    ("kh", "h"),
    ("ck", "k"),
    ("ch", "4"),
    ("c", "k"),
    ("ge", "je"),
    ("ph", "f"),
    ("th", "t"),
    ("ew", "yu"),
    ("w", "v"),
    ("y", "i"),
    ("x", "ks"),
    ("q", "k"),
    ("ts", "z"),
    ("ou", "u"),
    ("oo", "u"),
    ("ee", "i"),
]
_ALIKE = 0.45
# characters of one model input; longer sentences are cut: the model handles
# a few hundred tokens well and would cut off a longer output
_MAX_PIECE = 400
# strings per model load: the lock is held for seconds, not for a whole tree
_BATCH = 200
_SAME = 0.8


@lru_cache(maxsize=None)
def available(models_dir: str, target: str) -> bool:
    """Tell whether the model translating into `target` is installed."""
    if not models_dir or target not in MODELS:
        return False
    if not all(importlib.util.find_spec(m) for m in ("ctranslate2", "sentencepiece")):
        return False
    path = os.path.join(models_dir, MODELS[target])
    return os.path.isfile(os.path.join(path, "sentencepiece.model")) and os.path.isfile(
        os.path.join(path, "model", "model.bin")
    )


def _shorten(sentence: str) -> list[str]:
    """Cut a sentence too long for the model, at a comma if possible.

    Returns pieces at even indices and the spaces between them at odd ones.
    """
    if len(sentence) <= _MAX_PIECE:
        return [sentence]
    words = re.split(r"(\s+)", sentence)  # word, space, word, ...
    pieces: list[str] = []
    current = ""
    for i in range(0, len(words), 2):
        current += words[i]
        space = words[i + 1] if i + 1 < len(words) else ""
        full = len(current) >= _MAX_PIECE or (
            len(current) >= _MAX_PIECE // 2 and current.endswith((",", ";", ":"))
        )
        if full and space:
            pieces += [current, space]
            current = ""
        else:
            current += space
    return pieces + [current]


def _split(text: str) -> list[str]:
    """Split text into pieces: sentences at even indices, the glue at odd ones."""
    pieces: list[str] = []
    for i, line in enumerate(re.split(r"(\n+)", text)):
        if i % 2:
            pieces[-1] += line  # newlines are glue
            continue
        core = line.strip()
        lead = line[: len(line) - len(line.lstrip())]
        trail = line[len(line.rstrip()) :]
        if pieces:
            pieces[-1] += lead
        elif lead:
            pieces += ["", lead]
        start = 0
        for match in _SENTENCE_END.finditer(core):
            word = re.split(r"\W", core[: match.start()])[-1]
            # "г. Москва", "А.М. Шорин" are no sentence ends
            if len(word) < 3 and "." in match.group(1):
                continue
            pieces += _shorten(core[start : match.end(1)]) + [match.group(2)]
            start = match.end()
        pieces += _shorten(core[start:]) + [trail]
    return pieces


def _sounds(text: str) -> str:
    text = re.sub(r"[^a-z]", "", transliterate(text).lower())
    text = re.sub(r"(.)\1", r"\1", text)
    for letters, sound in _SOUNDS:
        text = text.replace(letters, sound)
    return text


def _spelled(source: str, result: str) -> bool:
    """Tell whether a proper name was spelled in the other script, not translated.

    Wanted: Москва -> Moscow, Haifa -> Хайфа. Not wanted: Красная Горка ->
    Red Hill, Сокол -> Falcon, Great Falls -> Великий водопад.
    """
    alike = SequenceMatcher(None, _sounds(source), _sounds(result)).ratio()
    words = [w for w in re.split(r"[\s-]+", source) if w]
    if all(w[:1].isupper() for w in words) and any(
        w[:1].islower() for w in re.split(r"[\s-]+", result) if len(w) > 2
    ):
        # a common noun now: "Little Rock" -> "Маленький рок"; but the
        # model also writes "haifa"
        return alike >= _SAME
    return alike >= _ALIKE


def _tidy(source: str, result: str, mode: str) -> str:
    """Undo habits of the model that do not suit labels and names."""
    result = result.strip()
    if not result:
        return source
    if (
        result.endswith(".")
        and not result.endswith("..")
        and not source.endswith(_END_PUNCTUATION)
    ):
        result = result[:-1]  # "Прозвище" -> "Nickname.", not wanted
    if source[:1].isupper() and result[:1].islower():
        result = result[0].upper() + result[1:]
    if mode == MODE_PLACE and all(
        w[:1].isupper() for w in re.split(r"[\s-]+", source) if w
    ):
        result = re.sub(r"(?:^|(?<=[\s-]))(\w)", lambda m: m.group(1).upper(), result)
    return result


class _Model:
    """A loaded translation model."""

    def __init__(self, models_dir: str, target: str):
        import ctranslate2  # pylint: disable=import-outside-toplevel
        import sentencepiece  # pylint: disable=import-outside-toplevel

        path = os.path.join(models_dir, MODELS[target])
        self.translator = ctranslate2.Translator(
            os.path.join(path, "model"),
            device="cpu",
            compute_type="int8",
            inter_threads=1,
            intra_threads=THREADS,
        )
        self.tokenizer = sentencepiece.SentencePieceProcessor(
            model_file=os.path.join(path, "sentencepiece.model")
        )

    def translate(self, texts: list[str]) -> list[str]:
        tokens = [self.tokenizer.encode(text, out_type=str) for text in texts]
        longest = max((len(t) for t in tokens), default=0)
        results = self.translator.translate_batch(
            tokens,
            max_batch_size=16,
            beam_size=2,
            max_decoding_length=min(1024, 2 * longest + 16),
            replace_unknowns=True,
        )
        return [self.tokenizer.decode(r.hypotheses[0]) for r in results]

    def close(self) -> None:
        self.translator.unload_model()


def _translate(models_dir: str, target: str, keys: list[Key]) -> dict[Key, str]:
    """Translate the source strings of keys with one target language."""
    plans = {}  # key -> pieces; at even indices the model input
    for key in keys:
        mode, _, source = key
        if mode == MODE_NAME:
            plans[key] = [_NAME_CARRIER.format(source)]
        elif mode == MODE_PLACE:
            plans[key] = [source, ""]
        else:
            plans[key] = _split(source)
    inputs = sorted({p for pieces in plans.values() for p in pieces[::2] if p.strip()})
    model = _Model(models_dir, target)
    try:
        outputs = dict(zip(inputs, model.translate(inputs)))
    finally:
        model.close()
    results = {}
    for key, pieces in plans.items():
        mode, _, source = key
        if mode == MODE_NAME:
            match = _NAME_ANSWER.fullmatch(outputs[pieces[0]].strip())
            name = match.group(1) if match else ""
            # anything but one Cyrillic word is no spelling of a name
            ok = _CYRILLIC.search(name) and _spelled(source, name)
            results[key] = name if ok else source
            continue
        if mode == MODE_PLACE and not _spelled(source, outputs[source]):
            # a place name translated word by word: keep the name, it is
            # spelled when shown (see `content._convert_text`)
            results[key] = source
            continue
        for i in range(0, len(pieces), 2):
            if pieces[i].strip():
                pieces[i] = _tidy(pieces[i], outputs[pieces[i]], mode)
        results[key] = "".join(pieces)
    return results


@contextmanager
def _exclusive(lock_path: str, blocking: bool):
    """Let one process at a time load a model: memory over speed.

    Yields whether the lock was taken; without `blocking`, False at once when
    another process has it.
    """
    if fcntl is None:
        yield True
        return
    with open(lock_path, "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def translate_missing(
    db_path: str, models_dir: str, keys: list[Key], blocking: bool = True
) -> Optional[int]:
    """Translate the keys not in the cache yet; return how many were done.

    Without `blocking`, returns None when another process is translating,
    so that a background worker is not held while it waits.
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    keys = list(dict.fromkeys(keys))
    count = 0
    for i in range(0, len(keys), _BATCH):
        batch = keys[i : i + _BATCH]
        with _exclusive(f"{db_path}.lock", blocking) as locked:
            if not locked:
                return None
            known, _ = store.lookup(db_path, batch)  # done while waiting?
            todo: dict[str, list[Key]] = {}
            for key in batch:
                if key not in known and available(models_dir, key[1]):
                    todo.setdefault(key[1], []).append(key)
            for target, target_keys in todo.items():
                start = time.monotonic()
                results = _translate(models_dir, target, target_keys)
                store.save(db_path, results)
                count += len(results)
                _LOG.info(
                    "Translated %d strings into %s in %.1f s",
                    len(results),
                    target,
                    time.monotonic() - start,
                )
    return count
