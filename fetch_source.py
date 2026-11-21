"""Fetch only the challenge's pinned SQLGlot source archive during image build."""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path
from urllib.request import urlopen

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{PIN}"
TARGET = Path("/opt/sqlglot")


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    with urlopen(URL, timeout=120) as response:
        archive = tarfile.open(fileobj=io.BytesIO(response.read()), mode="r:gz")
        members = archive.getmembers()
        root = members[0].name.split("/", 1)[0]
        for member in members:
            parts = member.name.split("/", 1)
            if len(parts) == 1 or parts[0] != root:
                continue
            relative = Path(parts[1])
            destination = (TARGET / relative).resolve()
            if TARGET.resolve() not in destination.parents and destination != TARGET.resolve():
                raise ValueError(f"unsafe source archive member: {member.name}")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is not None:
                    with source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
        (TARGET / ".source-revision").write_text(PIN, encoding="ascii")


if __name__ == "__main__":
    main()
