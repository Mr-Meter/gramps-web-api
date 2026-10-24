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

"""Download the translation models: python3 download.py <directory>

The Argos Translate packages ru_en and en_ru 1.9 (CTranslate2 format, about
370 MB together). Only model/ and sentencepiece.model are unpacked; the
package's sentence splitter is a pickle and not needed. Standard library
only: the Docker build runs this file before the package is installed.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

BASE_URL = os.environ.get("TRANSLATION_MODELS_URL", "https://argos-net.com/v1")
PACKAGES = {
    "ru_en": (
        "translate-ru_en-1_9.argosmodel",
        "e9ba8bf722d10a4a4c39f74289d5938fd47eac08dbe4ed0afd22d89445a5c3ac",
    ),
    "en_ru": (
        "translate-en_ru-1_9.argosmodel",
        "591d743ae103752b88ffc38785c50421320f4eff93c8967e0d3d2e14d4e27811",
    ),
}
KEEP = ("model/", "sentencepiece.model")


def fetch(directory: str, name: str, file_name: str, sha256: str) -> None:
    """Download one package and unpack its model to <directory>/<name>."""
    target = os.path.join(directory, name)
    if os.path.isfile(os.path.join(target, "model", "model.bin")):
        print(f"{name}: already there")
        return
    print(f"{name}: downloading {file_name} ...")
    with tempfile.TemporaryFile() as tmp:
        with urllib.request.urlopen(f"{BASE_URL}/{file_name}", timeout=60) as response:
            shutil.copyfileobj(response, tmp)
        tmp.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: tmp.read(1 << 20), b""):
            digest.update(chunk)
        if digest.hexdigest() != sha256:
            sys.exit(f"{file_name}: wrong checksum {digest.hexdigest()}")
        tmp.seek(0)
        partial = f"{target}.part"
        shutil.rmtree(partial, ignore_errors=True)
        with zipfile.ZipFile(tmp) as archive:
            for member in archive.infolist():
                # "translate-ru_en-1_9/model/model.bin" -> "model/model.bin"
                inner = member.filename.partition("/")[2]
                if member.is_dir() or not inner.startswith(KEEP):
                    continue
                if inner.startswith("/") or ".." in inner.split("/"):
                    sys.exit(f"{file_name}: bad path {member.filename}")
                dest = os.path.join(partial, inner)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with archive.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    os.replace(partial, target)
    print(f"{name}: done")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__.splitlines()[0])
    os.makedirs(sys.argv[1], exist_ok=True)
    for name, (file_name, sha256) in PACKAGES.items():
        fetch(sys.argv[1], name, file_name, sha256)


if __name__ == "__main__":
    main()
