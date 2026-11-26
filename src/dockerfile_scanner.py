"""Locate Dockerfiles in a checked-out repository."""
from __future__ import annotations

import os
import re
from pathlib import Path

# Dockerfile, Dockerfile.dev, Dockerfile-prod, dev.Dockerfile, Dockerfile_test, app.dockerfile
_NAME_RE = re.compile(r"^(?:Dockerfile(?:[._-][\w.\-]+)?|[\w.\-]+\.[Dd]ockerfile)$", re.IGNORECASE)
_IGNORED_SUFFIXES = (".md", ".txt", ".json", ".yml", ".yaml", ".sh", ".bak", ".orig", ".rej", ".swp", ".tmp")
_IGNORED_NAMES = {"dockerfile.dockerignore"}


def is_dockerfile_name(name: str) -> bool:
    low = name.lower()
    if low in _IGNORED_NAMES or low.endswith(_IGNORED_SUFFIXES) or low.endswith(".dockerignore"):
        return False
    return bool(_NAME_RE.match(name))


def find_dockerfiles(root: str | Path, max_depth: int = 4, ignore_dirs: list[str] | None = None) -> list[str]:
    """Return POSIX-style relative paths of Dockerfiles.

    max_depth counts sub-folder levels below the root: root files are depth 0,
    ``a/b/c/d/Dockerfile`` is depth 4.
    """
    root = Path(root)
    ignore = {d.lower() for d in (ignore_dirs or [])}
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        depth = 0 if str(rel_dir) == "." else len(rel_dir.parts)
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in ignore and not os.path.islink(os.path.join(dirpath, d)))
        if depth >= max_depth:
            dirnames[:] = []           # do not descend further than max_depth
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if is_dockerfile_name(fn) and os.path.isfile(full) and not os.path.islink(full):
                found.append((rel_dir / fn).as_posix())
    return sorted(found)
