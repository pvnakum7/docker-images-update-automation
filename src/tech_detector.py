"""Technology-stack detection from repository manifests, plus image/technology compatibility rules."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from .models import TechFinding
from .version_manager import family_of

NODE, REACT, ANGULAR = "Node.js", "React", "Angular"
PYTHON, DOTNET, GO = "Python", ".NET", "Go"
JAVA_MAVEN, JAVA_GRADLE, JAVA = "Java Maven", "Java Gradle", "Java"
ALL_TECHS = [NODE, REACT, ANGULAR, PYTHON, DOTNET, GO, JAVA_MAVEN, JAVA_GRADLE, JAVA]

DEFAULT_IGNORE = {".git", "node_modules", "vendor", ".venv", "venv", "env", "__pycache__", ".tox", "dist",
                  "build", "target", ".idea", ".gradle", "bin", "obj", ".terraform"}


# ------------------------------------------------------------------ constraints
def parse_constraint(spec: str) -> Optional[dict]:
    """Parse '>=18', '^20', '20.x', '>=3.9,<4', '~20.1' into {'min': tuple|None, 'max_excl': tuple|None}."""
    if not spec or "||" in spec:
        return None
    mn: Optional[tuple] = None
    mx: Optional[tuple] = None
    parts = [p for p in re.split(r"[,\s]+", spec.strip()) if p]
    for p in parts:
        m = re.match(r"^(>=|<=|>|<|==|=|\^|~=|~)?v?(\d+(?:\.\d+)*)(?:\.[xX*])?$", p)
        if not m:
            return None
        op, ver = m.group(1) or "=", tuple(int(x) for x in m.group(2).split("."))
        if op in {">=", ">"}:
            mn = ver
        elif op in {"<"}:
            mx = ver
        elif op == "<=":
            mx = ver[:-1] + (ver[-1] + 1,)
        elif op in {"^"}:
            mn, mx = ver, (ver[0] + 1,)
        elif op in {"~", "~="}:
            mn = ver
            mx = ver[:-1] + (ver[-1] + 1,) if len(ver) > 1 else (ver[0] + 1,)
        else:   # exact / "20.x"
            mn = ver
            mx = ver[:-1] + (ver[-1] + 1,)
    return {"min": mn, "max_excl": mx}


def satisfies(constraint: dict, line: tuple[int, ...]) -> bool:
    L = len(line)
    mn, mx = constraint.get("min"), constraint.get("max_excl")
    if mn and line < mn[:L]:
        return False
    if mx and not line < mx[:L]:
        return False
    return True


def _read(path: Path, limit: int = 512_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _mk(name, directory, indicators, hint=None, constraint=None, source=None) -> TechFinding:
    f = TechFinding(name=name, indicators=sorted(indicators), directory=directory)
    if hint:
        f.hints.append(hint)
    if constraint:
        f.constraints = {**constraint, "source": source}
    return f


# ------------------------------------------------------------------ per-directory detectors
def _detect_dir(rel: str, d: Path, files: set[str]) -> list[TechFinding]:
    out: list[TechFinding] = []

    def ind(*names):
        return [(f"{rel}/{n}" if rel != "." else n) for n in names if n in files]

    # ---- Node / React / Angular
    node_files = ind("package.json", "package-lock.json", ".nvmrc", "yarn.lock", "pnpm-lock.yaml", ".node-version")
    deps: set[str] = set()
    engines = None
    if "package.json" in files:
        try:
            pkg = json.loads(_read(d / "package.json"))
            deps = set((pkg.get("dependencies") or {})) | set((pkg.get("devDependencies") or {})) \
                | set((pkg.get("peerDependencies") or {}))
            engines = (pkg.get("engines") or {}).get("node")
        except (ValueError, AttributeError):
            pass
    if node_files:
        cons, hint, src = None, None, None
        if ".nvmrc" in files:
            m = re.match(r"\s*v?(\d+(?:\.\d+)*)", _read(d / ".nvmrc"))
            if m:
                ver = tuple(int(x) for x in m.group(1).split("."))
                cons, hint, src = {"min": ver[:1], "max_excl": (ver[0] + 1,)}, f"node {m.group(1)} (.nvmrc)", ".nvmrc"
        if not cons and engines:
            c = parse_constraint(engines)
            if c:
                cons, hint, src = c, f"node {engines} (package.json engines)", "package.json engines"
        out.append(_mk(NODE, rel, node_files, hint, cons, src))
    if "react" in deps or "react-dom" in deps:
        out.append(_mk(REACT, rel, ind("package.json") + ["dependency: react"]))
    if "angular.json" in files or "@angular/core" in deps:
        out.append(_mk(ANGULAR, rel, ind("angular.json", "package.json") + (["dependency: @angular/core"] if "@angular/core" in deps else [])))

    # ---- Python
    py_files = ind("requirements.txt", "pyproject.toml", "Pipfile", "setup.py", "setup.cfg", ".python-version", "poetry.lock")
    if py_files:
        cons, hint, src = None, None, None
        if "pyproject.toml" in files:
            m = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']', _read(d / "pyproject.toml"))
            if m and (c := parse_constraint(m.group(1))):
                cons, hint, src = c, f"python {m.group(1)} (pyproject.toml)", "pyproject.toml requires-python"
        if not cons and ".python-version" in files:
            m = re.match(r"\s*(\d+(?:\.\d+)*)", _read(d / ".python-version"))
            if m:
                v = tuple(int(x) for x in m.group(1).split("."))[:2]
                cons, hint, src = {"min": v, "max_excl": v[:-1] + (v[-1] + 1,)}, f"python {m.group(1)} (.python-version)", ".python-version"
        if not cons and "Pipfile" in files:
            m = re.search(r'python_version\s*=\s*["\'](\d+(?:\.\d+)*)["\']', _read(d / "Pipfile"))
            if m:
                v = tuple(int(x) for x in m.group(1).split("."))[:2]
                cons, hint, src = {"min": v, "max_excl": v[:-1] + (v[-1] + 1,)}, f"python {m.group(1)} (Pipfile)", "Pipfile"
        out.append(_mk(PYTHON, rel, py_files, hint, cons, src))

    # ---- .NET
    cs = sorted(f for f in files if f.lower().endswith((".csproj", ".fsproj", ".vbproj", ".sln")))
    dn_files = [(f"{rel}/{n}" if rel != "." else n) for n in cs] + ind("global.json")
    if dn_files:
        cons, hint, src = None, None, None
        for n in cs:
            m = re.search(r"<TargetFramework>net(?:coreapp)?(\d+)(?:\.(\d+))?", _read(d / n))
            if m:
                maj = int(m.group(1))
                cons, hint, src = {"min": (maj,), "max_excl": None}, f"target framework {m.group(0).split('>')[-1]} ({n})", n
                break
        if not cons and "global.json" in files:
            m = re.search(r'"version"\s*:\s*"(\d+)\.', _read(d / "global.json"))
            if m:
                cons, hint, src = {"min": (int(m.group(1)),), "max_excl": None}, f"sdk {m.group(1)}.x (global.json)", "global.json"
        out.append(_mk(DOTNET, rel, dn_files, hint, cons, src))

    # ---- Go
    go_files = ind("go.mod", "go.sum", "go.work")
    if go_files:
        cons, hint, src = None, None, None
        if "go.mod" in files:
            m = re.search(r"^go\s+(\d+)\.(\d+)", _read(d / "go.mod"), re.M)
            if m:
                cons, hint, src = {"min": (int(m.group(1)), int(m.group(2))), "max_excl": None}, f"go {m.group(1)}.{m.group(2)} (go.mod)", "go.mod"
        out.append(_mk(GO, rel, go_files, hint, cons, src))

    # ---- Java
    mvn = ind("pom.xml", "mvnw")
    grd = ind("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradlew")
    jdk = ind(".java-version", ".sdkmanrc")
    cons, hint, src = None, None, None

    def _java_ver(s: str) -> Optional[int]:
        s = s.strip()
        if s.startswith("1."):
            s = s[2:]
        m = re.match(r"(\d+)", s)
        return int(m.group(1)) if m else None

    if "pom.xml" in files:
        t = _read(d / "pom.xml")
        m = re.search(r"<(?:maven\.compiler\.release|java\.version|maven\.compiler\.source|maven\.compiler\.target)>\s*([\d.]+)\s*<", t)
        if m and (v := _java_ver(m.group(1))):
            cons, hint, src = {"min": (v,), "max_excl": None}, f"java {v} (pom.xml)", "pom.xml"
    if not cons:
        for g in ("build.gradle", "build.gradle.kts"):
            if g in files:
                t = _read(d / g)
                m = re.search(r"JavaLanguageVersion\.of\(\s*(\d+)\s*\)|VERSION_(\d+)|(?:source|target)Compatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?([\d.]+)", t)
                if m:
                    v = _java_ver(next(x for x in m.groups() if x))
                    if v:
                        cons, hint, src = {"min": (v,), "max_excl": None}, f"java {v} ({g})", g
                        break
    if not cons and ".java-version" in files:
        if v := _java_ver(_read(d / ".java-version")):
            cons, hint, src = {"min": (v,), "max_excl": None}, f"java {v} (.java-version)", ".java-version"
    if mvn:
        out.append(_mk(JAVA_MAVEN, rel, mvn, hint, cons, src))
    if grd:
        out.append(_mk(JAVA_GRADLE, rel, grd, hint, cons, src))
    if mvn or grd or jdk:
        out.append(_mk(JAVA, rel, mvn + grd + jdk, hint, cons, src))
    return out


def detect_technologies(repo_root: str | Path, max_depth: int = 6, ignore_dirs=None) -> list[TechFinding]:
    root = Path(repo_root)
    ignore = {d.lower() for d in (ignore_dirs or DEFAULT_IGNORE)}
    findings: list[TechFinding] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        rel_s = "." if str(rel) == "." else rel.as_posix()
        depth = 0 if rel_s == "." else len(rel.parts)
        dirnames[:] = [d for d in sorted(dirnames) if d.lower() not in ignore]
        if depth >= max_depth:
            dirnames[:] = []
        findings += _detect_dir(rel_s, Path(dirpath), set(filenames))
    return findings


def summarize(findings: list[TechFinding]) -> dict[str, list[TechFinding]]:
    by: dict[str, list[TechFinding]] = {}
    for f in findings:
        by.setdefault(f.name, []).append(f)
    return by


def techs_for_dockerfile(dockerfile_rel: str, findings: list[TechFinding]) -> list[TechFinding]:
    """Findings that plausibly belong to a Dockerfile: own dir + ancestors; root Dockerfile -> whole repo."""
    d = os.path.dirname(dockerfile_rel) or "."
    if d == ".":
        return list(findings)
    chain = {"."}
    parts = d.split("/")
    for i in range(1, len(parts) + 1):
        chain.add("/".join(parts[:i]))
    scoped = [f for f in findings if f.directory in chain]
    return scoped or list(findings)


# ------------------------------------------------------------------ image <-> technology compatibility
_WEB = {"nginx", "httpd", "caddy", "apache"}


def image_techs(repository: str) -> Optional[set[str]]:
    repo = repository.removeprefix("library/").lower()
    fam = family_of(repo)
    if fam == "node" or repo in {"node"}:
        return {NODE, REACT, ANGULAR}
    if fam == "python":
        return {PYTHON}
    if fam in {"golang", "go"}:
        return {GO}
    if "dotnet/" in repo:
        return {DOTNET}
    if fam == "maven":
        return {JAVA_MAVEN, JAVA}
    if fam == "gradle":
        return {JAVA_GRADLE, JAVA}
    if fam in {"eclipse-temurin", "openjdk", "amazoncorretto", "ibm-semeru-runtimes", "zulu-openjdk",
               "liberica-openjdk-debian", "liberica-openjdk-alpine", "sapmachine", "temurin"}:
        return {JAVA, JAVA_MAVEN, JAVA_GRADLE}
    return None


def assess_major_compat(repository: str, new_line: tuple[int, ...], techs: list[TechFinding]) -> tuple[bool, str]:
    """(established, remark). Only True when technology matches AND declared constraints allow the new line."""
    fam_techs = image_techs(repository)
    fam = family_of(repository)
    if fam in _WEB:
        return False, f"{fam} is a web-server runtime; configuration compatibility with a new major cannot be established automatically"
    if fam_techs is None:
        return False, "image is not mapped to a detected application technology; compatibility cannot be established"
    matching = [t for t in techs if t.name in fam_techs]
    if not matching:
        return False, f"{fam} image but no matching technology detected for this Dockerfile"
    with_c = [t for t in matching if t.constraints]
    if not with_c:
        return False, "technology detected but no declared runtime version to verify against"
    for t in with_c:
        if not satisfies(t.constraints, new_line):
            return False, f"project declares {t.hints[0] if t.hints else t.constraints.get('source')} which does not allow the new version"
    src = ", ".join(sorted({t.constraints.get('source', '?') for t in with_c}))
    return True, f"declared runtime constraint ({src}) allows the new version; run full build/tests before merging"
