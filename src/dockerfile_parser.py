"""Line-based, Dockerfile-aware parser and surgical FROM rewriter.

Handles: line continuations, comments (also inside continuations), parser directives,
heredocs (so `FROM` text inside a RUN heredoc is never mistaken for an instruction),
multi-stage builds, ``--platform`` flags, ``AS`` aliases, global ``ARG`` substitution,
digests and custom registries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .models import FromInstruction, ImageRef


class InvalidImageReference(ValueError):
    pass


_DOCKER_HUB_HOSTS = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}
_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}$")
_DIGEST_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[+._-][A-Za-z][A-Za-z0-9]*)*:[0-9a-fA-F]{32,}$")


def parse_image_ref(ref: str) -> ImageRef:
    """Parse ``[registry/]repo[:tag][@digest]``. Raises InvalidImageReference."""
    raw = ref.strip()
    if not raw:
        raise InvalidImageReference("empty image reference")
    if any(c.isspace() for c in raw) or "$" in raw:
        raise InvalidImageReference(f"unexpected characters in image reference {raw!r}")
    rest, digest = raw, None
    if "@" in rest:
        rest, digest = rest.split("@", 1)
        if not _DIGEST_RE.match(digest):
            raise InvalidImageReference(f"invalid digest in {raw!r}")
    tag = None
    last_slash = rest.rfind("/")
    colon = rest.rfind(":")
    if colon > last_slash:
        rest, tag = rest[:colon], rest[colon + 1:]
        if not _TAG_RE.match(tag):
            raise InvalidImageReference(f"invalid tag {tag!r} in {raw!r}")
    name_as_written = rest
    parts = rest.split("/")
    registry, explicit = "docker.io", False
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, explicit = parts[0].lower(), True
        parts = parts[1:]
    if registry in _DOCKER_HUB_HOSTS:
        registry = "docker.io"
    if not parts or any(not p for p in parts):
        raise InvalidImageReference(f"empty path component in {raw!r}")
    for p in parts:
        if not _COMPONENT_RE.match(p):
            raise InvalidImageReference(f"invalid repository component {p!r} in {raw!r}")
    if registry == "docker.io" and len(parts) == 1:
        parts = ["library"] + parts
    return ImageRef(raw=raw, registry=registry, repository="/".join(parts), tag=tag,
                    digest=digest, name_as_written=name_as_written)


# ----------------------------------------------------------------------------
# Instruction tokenising
# ----------------------------------------------------------------------------
@dataclass
class _Instr:
    keyword: str
    text: str          # arguments joined (continuations removed)
    start: int
    end: int


_HEREDOC_RE = re.compile(r"<<(-?)\s*(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z_][A-Za-z0-9_]*))")


def _escape_char(lines: list[str]) -> str:
    for ln in lines:
        s = ln.strip()
        if not s.startswith("#"):
            if s:
                break
            continue
        m = re.match(r"#\s*escape\s*=\s*(\\|`)\s*$", s, re.I)
        if m:
            return m.group(1)
    return "\\"


def tokenize_instructions(lines: list[str]) -> list[_Instr]:
    esc = _escape_char(lines)
    out: list[_Instr] = []
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        start = i
        buf: list[str] = []
        last = i
        while i < n:
            line = lines[i].rstrip("\r\n")
            if i != start and line.strip().startswith("#"):
                i += 1                      # comment inside continuation
                continue
            if not line.strip() and i != start:
                i += 1                      # empty line inside continuation
                continue
            s = line.rstrip()
            last = i
            if s.endswith(esc):
                buf.append(s[:-1])
                i += 1
                continue
            buf.append(s)
            i += 1
            break
        full = " ".join(b.strip() for b in buf).strip()
        parts = full.split(None, 1)
        keyword = parts[0].upper() if parts else ""
        text = parts[1] if len(parts) > 1 else ""
        end = last
        if keyword in {"RUN", "COPY", "ADD"}:
            terms = [(m.group(1) == "-", m.group(2) or m.group(3) or m.group(4)) for m in _HEREDOC_RE.finditer(text)]
            for strip_tabs, word in terms:
                while i < n:
                    cand = lines[i].rstrip("\r\n")
                    cand = cand.lstrip("\t") if strip_tabs else cand
                    i += 1
                    end = i - 1
                    if cand == word:
                        break
        out.append(_Instr(keyword, text, start, end))
    return out


# ----------------------------------------------------------------------------
# ARG substitution
# ----------------------------------------------------------------------------
_VAR_RE = re.compile(
    r"\$(?:\{(?P<b>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?[-+])(?P<w>[^}]*))?\}|(?P<n>[A-Za-z_][A-Za-z0-9_]*))")
_BUILTIN_ARGS = {"BUILDPLATFORM", "BUILDOS", "BUILDARCH", "BUILDVARIANT", "TARGETPLATFORM", "TARGETOS",
                 "TARGETARCH", "TARGETVARIANT"}


@dataclass
class Substitution:
    value: Optional[str]
    reason: Optional[str] = None
    variables: list[str] = field(default_factory=list)
    simple_vars: list[str] = field(default_factory=list)   # plain $V / ${V} (no operator)


def substitute(token: str, args: dict[str, Optional[str]]) -> Substitution:
    variables: list[str] = []
    simple: list[str] = []
    failure: list[str] = []

    def repl(m: re.Match) -> str:
        name = m.group("b") or m.group("n")
        op, word = m.group("op"), m.group("w")
        variables.append(name)
        if name in _BUILTIN_ARGS:
            failure.append(f"uses build-time variable ${name}")
            return ""
        val = args.get(name)
        if op is None:
            simple.append(name)
            if val is None or val == "":
                failure.append(f"ARG {name} has no default value (supplied at build time)")
                return ""
            return val
        if op == ":-":
            return val if val else (word or "")
        if op == "-":
            return val if val is not None else (word or "")
        if op == ":+":
            return (word or "") if val else ""
        if op == "+":
            return (word or "") if val is not None else ""
        failure.append(f"unsupported expansion for {name}")  # pragma: no cover
        return ""

    resolved = _VAR_RE.sub(repl, token)
    if failure:
        return Substitution(None, "; ".join(dict.fromkeys(failure)), variables, simple)
    return Substitution(resolved, None, variables, simple)


# ----------------------------------------------------------------------------
# Public parse API
# ----------------------------------------------------------------------------
@dataclass
class ParsedDockerfile:
    lines: list[str]
    froms: list[FromInstruction]
    global_args: dict[str, Optional[str]]
    arg_lines: dict[str, int]
    raw_text: str


_ARG_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?$")


def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_dockerfile(text: str) -> ParsedDockerfile:
    lines = text.splitlines(keepends=True)
    instrs = tokenize_instructions(lines)
    global_args: dict[str, Optional[str]] = {}
    arg_lines: dict[str, int] = {}
    froms: list[FromInstruction] = []
    aliases: set[str] = set()
    seen_from = False
    for ins in instrs:
        if ins.keyword == "ARG" and not seen_from:
            for item in _split_args(ins.text):
                m = _ARG_RE.match(item)
                if m:
                    global_args[m.group(1)] = _strip_quotes(m.group(2)) if m.group(2) is not None else None
                    arg_lines[m.group(1)] = ins.start
            continue
        if ins.keyword != "FROM":
            continue
        seen_from = True
        toks = ins.text.split()
        platform = None
        while toks and toks[0].startswith("--"):
            flag = toks.pop(0)
            if flag.lower().startswith("--platform"):
                platform = flag.split("=", 1)[1] if "=" in flag else None
        if not toks:
            froms.append(FromInstruction(len(froms) + 1, ins.start, ins.end, platform, "", None, None,
                                         "FROM without an image"))
            continue
        token = toks[0]
        alias = None
        if len(toks) >= 3 and toks[1].upper() == "AS":
            alias = toks[2]
        fi = FromInstruction(len(froms) + 1, ins.start, ins.end, platform, token, alias, None)
        if token.lower() in aliases:
            fi.is_stage_ref = True
            fi.resolved_image = token
        elif token.lower() == "scratch":
            fi.is_scratch = True
            fi.resolved_image = token
        else:
            sub = substitute(token, global_args)
            fi.resolved_image = sub.value
            fi.unresolved_reason = sub.reason
            if sub.variables and sub.value is not None:
                _prepare_arg_edit(fi, token, sub, global_args, arg_lines, instrs)
        if alias:
            aliases.add(alias.lower())
        froms.append(fi)
    return ParsedDockerfile(lines, froms, global_args, arg_lines, text)


def _split_args(text: str) -> list[str]:
    return re.findall(r"""(?:[^\s"']+|"[^"]*"|'[^']*')+""", text)


def _prepare_arg_edit(fi: FromInstruction, token: str, sub: Substitution, args, arg_lines, instrs) -> None:
    """Decide whether the single ARG used in this FROM can be edited instead of the FROM line."""
    occurrences = _VAR_RE.findall(token)
    if len(occurrences) != 1 or len(sub.simple_vars) != 1:
        fi.arg_edit_blocked = "FROM composes several variables or uses expansion operators"
        return
    name = sub.simple_vars[0]
    m = _VAR_RE.search(token)
    fi.arg_name = name
    fi.arg_line = arg_lines.get(name)
    fi.arg_prefix = token[:m.start()]
    fi.arg_suffix = token[m.end():]
    pat = re.compile(r"\$\{?" + re.escape(name) + r"(?![A-Za-z0-9_])")
    uses = 0
    for ins in instrs:
        if ins.keyword == "ARG" and ins.start == fi.arg_line:
            continue
        uses += len(pat.findall(" ".join([ins.text])))
    if uses != 1:
        fi.arg_edit_blocked = f"ARG {name} is referenced {uses} times in the file (not only by this FROM)"


def _replace_in_text(chunk: str, old: str, new: str) -> str:
    return chunk.replace(old, new, 1)


def rewrite_from(parsed: ParsedDockerfile, fi: FromInstruction, new_resolved_image: str) -> list[str]:
    """Return new line list with exactly this FROM (or its ARG default) rewritten.

    Everything else - comments, indentation, line endings, flags, aliases - is untouched.
    Raises ValueError if the target text cannot be located unambiguously.
    """
    lines = list(parsed.lines)
    if fi.arg_name:
        if fi.arg_edit_blocked or fi.arg_line is None:
            raise ValueError(fi.arg_edit_blocked or "ARG definition not found")
        if not (new_resolved_image.startswith(fi.arg_prefix) and new_resolved_image.endswith(fi.arg_suffix)):
            raise ValueError("new image does not fit the ARG composition")
        new_val = new_resolved_image[len(fi.arg_prefix): len(new_resolved_image) - len(fi.arg_suffix)]
        old_val = parsed.global_args.get(fi.arg_name) or ""
        ln = lines[fi.arg_line]
        pat = re.compile(r"(\bARG\s+(?:[^\n]*?\s)?" + re.escape(fi.arg_name) + r"=)([\"']?)" + re.escape(old_val) + r"([\"']?)")
        new_ln, cnt = pat.subn(lambda m: f"{m.group(1)}{m.group(2)}{new_val}{m.group(3)}", ln, count=1)
        if cnt != 1:
            raise ValueError(f"could not locate default value of ARG {fi.arg_name}")
        lines[fi.arg_line] = new_ln
        return lines

    chunk = "".join(lines[fi.start_line: fi.end_line + 1])
    m = re.match(r"^(\s*FROM(?:[\s\\`]+--\S+)*[\s\\`]+)(\S+)", chunk, re.I)
    if not m or m.group(2) != fi.image_token:
        raise ValueError("could not locate image token in FROM instruction")
    new_chunk = chunk[:m.start(2)] + new_resolved_image + chunk[m.end(2):]
    new_lines = new_chunk.splitlines(keepends=True)
    lines[fi.start_line: fi.end_line + 1] = new_lines
    return lines
