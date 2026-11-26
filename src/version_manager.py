"""Tag parsing, stability filtering and upgrade selection.

Terminology
-----------
* *prefix*   - leading ``v`` (kept as-is, must match to be a candidate)
* *nums*     - numeric version components, e.g. ``(22, 22, 0)``
* *suffix*   - everything after the version, e.g. ``-slim`` or ``-alpine3.20``. This is the
               **variant**. It must match exactly, so slim/alpine/bookworm/... is always preserved.
               A leading ``_<build>`` (eclipse-temurin ``17.0.9_9-jre``) is a build number, not part of the variant.
* *line*     - the first N numeric components that define a "major line". N is 1 for most images
               (node 22, nginx 1, postgres 16) but 2 for images whose minor releases are the
               breaking ones (python 3.12 -> 3.13, golang 1.22 -> 1.23, php 8.2 -> 8.3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

_TAG_RE = re.compile(
    r"^(?P<prefix>[vV]?)(?P<nums>\d+(?:\.\d+){0,3})(?P<suffix>(?:[-_+.]?[A-Za-z][\w.\-+]*|[-_+][\w.\-+]+)?)$")

_UNSTABLE_TOKEN_RE = re.compile(
    r"^(alpha|beta|rc|nightly|dev|devel|preview|snapshot|canary|edge|test|testing|unstable|pre|early|next|"
    r"experimental|ea|main|master|latest|a|b|m|cr|milestone|insiders?)\d*$", re.I)

# Anything with a first component above this is treated as a date/build number, not a version
MAX_SANE_MAJOR = 999


@dataclass(frozen=True)
class ParsedTag:
    raw: str
    prefix: str
    nums: tuple[int, ...]
    suffix: str

    @property
    def variant(self) -> str:
        """Suffix without a leading build number: ``_9-jre`` and ``_11-jre`` are both the ``-jre`` variant."""
        return re.sub(r"^[_+]\d+", "", self.suffix)

    @property
    def build(self) -> Optional[int]:
        m = re.match(r"^[_+](\d+)", self.suffix)
        return int(m.group(1)) if m else None

    @property
    def padded(self) -> tuple[int, ...]:
        return self.nums + (0,) * (4 - len(self.nums))

    def line(self, depth: int) -> tuple[int, ...]:
        return self.nums[:depth]

    @property
    def is_stable(self) -> bool:
        tokens = [t for t in re.split(r"[-_.+]", self.suffix) if t]
        return not any(_UNSTABLE_TOKEN_RE.match(t) for t in tokens)


def parse_tag(tag: str) -> Optional[ParsedTag]:
    m = _TAG_RE.match(tag or "")
    if not m:
        return None
    nums = tuple(int(x) for x in m.group("nums").split("."))
    return ParsedTag(tag, m.group("prefix"), nums, m.group("suffix") or "")


def is_stable_tag(tag: str) -> bool:
    p = parse_tag(tag)
    return bool(p and p.is_stable)


def family_of(repository: str) -> str:
    """Last path component, used as the image family key (library/node -> node)."""
    return repository.rsplit("/", 1)[-1].lower()


def line_depth(repository: str, config: dict) -> int:
    depths = config.get("release_line_depth", {})
    return int(depths.get(family_of(repository), depths.get("default", 1)))


@dataclass
class Selection:
    tag: Optional[str]
    reason: str = ""
    candidates: int = 0


def _eligible(current: ParsedTag, tags: Iterable[str]) -> list[ParsedTag]:
    out = []
    for t in tags:
        p = parse_tag(t)
        if (p and p.prefix == current.prefix and p.variant == current.variant
                and (p.build is None) == (current.build is None) and p.is_stable
                and p.nums[0] <= MAX_SANE_MAJOR):
            out.append(p)
    return out


def _best(cands: list[ParsedTag], current: ParsedTag, precision: str) -> Optional[ParsedTag]:
    if not cands:
        return None
    pool = cands
    if precision == "match_current":
        same = [c for c in cands if len(c.nums) == len(current.nums)]
        pool = same or cands
    # highest numeric version first; on ties prefer the more fully specified tag
    return max(pool, key=lambda c: (c.padded, c.build or 0, len(c.nums)))


def select_same_major(current_tag: str, tags: Iterable[str], depth: int = 1,
                      precision: str = "full") -> Selection:
    """Strategy A: newest stable tag inside the current major line, same variant, never a downgrade."""
    cur = parse_tag(current_tag)
    if cur is None:
        return Selection(None, "current tag is not a versioned tag")
    if not cur.is_stable:
        return Selection(None, "current tag is a pre-release/unstable tag")
    if cur.nums[0] > MAX_SANE_MAJOR:
        return Selection(None, "current tag looks date/build based, not semantic")
    L = min(depth, len(cur.nums))
    cur_line = cur.nums[:L]
    cands = [c for c in _eligible(cur, tags) if len(c.nums) >= L and c.nums[:L] == cur_line]
    best = _best(cands, cur, precision)
    if best is None:
        return Selection(None, "no stable tag with the same variant found in the registry", 0)
    if best.padded < cur.padded:                       # never downgrade
        return Selection(None, "newest matching tag is older than current (downgrade prevented)", len(cands))
    return Selection(best.raw, "", len(cands))


def select_new_major(current_tag: str, tags: Iterable[str], depth: int = 1, precision: str = "full",
                     parity: Optional[str] = None) -> Selection:
    """Strategy B: newest stable tag in the newest major line above the current one, same variant."""
    cur = parse_tag(current_tag)
    if cur is None:
        return Selection(None, "current tag is not a versioned tag")
    if not cur.is_stable:
        return Selection(None, "current tag is a pre-release/unstable tag")
    if cur.nums[0] > MAX_SANE_MAJOR:
        return Selection(None, "current tag looks date/build based, not semantic")
    L = min(depth, len(cur.nums))
    cur_line = cur.nums[:L]
    cands = [c for c in _eligible(cur, tags) if len(c.nums) >= L and c.nums[:L] > cur_line]
    if parity in {"even", "odd"}:
        want = 0 if parity == "even" else 1
        cands = [c for c in cands if c.nums[0] % 2 == want]
    if not cands:
        return Selection(None, "no newer stable major version with the same variant found", 0)
    top_line = max(c.nums[:L] for c in cands)
    best = _best([c for c in cands if c.nums[:L] == top_line], cur, precision)
    return Selection(best.raw if best else None, "", len(cands))


def line_str(nums: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in nums)


def same_major_line(a: str, b: str, depth: int = 1) -> bool:
    pa, pb = parse_tag(a), parse_tag(b)
    if not pa or not pb:
        return False
    L = min(depth, len(pa.nums), len(pb.nums))
    return pa.nums[:L] == pb.nums[:L]


def is_downgrade(old_tag: str, new_tag: str) -> bool:
    po, pn = parse_tag(old_tag), parse_tag(new_tag)
    return bool(po and pn and pn.padded < po.padded)
