"""Post-modification validation: Dockerfile syntax (buildx --check or static), optional docker build,
and opt-in technology-specific build checks that only run for detected technologies."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .dockerfile_parser import parse_dockerfile, tokenize_instructions
from .logger import get_logger, redact
from .models import TechFinding

log = get_logger("validator")

PASSED, FAILED_V, NOT_EXECUTED, NOT_RUN = "PASSED", "FAILED", "NOT_EXECUTED", "NOT_RUN"

_VALID_KEYWORDS = {"FROM", "RUN", "CMD", "LABEL", "MAINTAINER", "EXPOSE", "ENV", "ADD", "COPY", "ENTRYPOINT",
                   "VOLUME", "USER", "WORKDIR", "ARG", "ONBUILD", "STOPSIGNAL", "HEALTHCHECK", "SHELL"}
_SECRET_ENV = ("TOKEN", "PASSWORD", "SECRET", "SMTP_", "EMAIL_", "CREDENTIAL", "PAT")

Runner = Callable[..., subprocess.CompletedProcess]

# Messages that mean "the tool/daemon is unavailable", NOT "the Dockerfile is bad"
_DAEMON_DOWN_RE = re.compile(
    r"Cannot connect to the Docker daemon|failed to connect to the docker API|is the docker daemon running|"
    r"dial unix .*docker\.sock|permission denied while trying to connect|unknown flag: --check|"
    r"no builder|buildx.* is not a docker command|connection refused", re.I)


@dataclass
class ValidationResult:
    status: str = NOT_RUN                 # PASSED | FAILED | NOT_EXECUTED | NOT_RUN
    summary: str = ""
    details: list[str] = field(default_factory=list)
    not_executed_reasons: list[str] = field(default_factory=list)

    def merge(self, other: "ValidationResult") -> None:
        order = {FAILED_V: 3, NOT_EXECUTED: 2, PASSED: 1, NOT_RUN: 0}
        if order[other.status] > order[self.status]:
            self.status = other.status
        self.details += other.details
        self.not_executed_reasons += other.not_executed_reasons


def static_check(text: str) -> list[str]:
    """Minimal built-in syntax validation used when buildx is unavailable. Returns a list of problems."""
    problems: list[str] = []
    lines = text.splitlines()
    instrs = tokenize_instructions(text.splitlines(keepends=True))
    if not instrs:
        return ["Dockerfile has no instructions"]
    seen_from = False
    for ins in instrs:
        if ins.keyword not in _VALID_KEYWORDS:
            problems.append(f"line {ins.start + 1}: unknown instruction {ins.keyword!r}")
        if ins.keyword == "FROM":
            seen_from = True
            if not ins.text.strip():
                problems.append(f"line {ins.start + 1}: FROM requires an image")
        elif not seen_from and ins.keyword != "ARG":
            problems.append(f"line {ins.start + 1}: {ins.keyword} before first FROM")
    if not seen_from:
        problems.append("no FROM instruction found")
    if lines and lines[-1].rstrip().endswith("\\"):
        problems.append("file ends with a dangling line continuation")
    parsed = parse_dockerfile(text)
    for f in parsed.froms:
        if not f.image_token:
            problems.append(f"line {f.start_line + 1}: FROM without image")
    return problems


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in _SECRET_ENV)}
    env["CI"] = "true"
    return env


class Validator:
    def __init__(self, settings: dict, runner: Runner = subprocess.run, which: Callable = shutil.which):
        self.cfg = settings["validation"]
        self.runner = runner
        self.which = which
        self._buildx_ok: Optional[bool] = None

    # ------------------------------------------------------------------ helpers
    def _run(self, cmd: list[str], cwd: Path, timeout: int, env: Optional[dict] = None):
        return self.runner(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           env=env or _clean_env(), check=False)

    def buildx_available(self) -> bool:
        if self._buildx_ok is None:
            if not self.which("docker"):
                self._buildx_ok = False
            else:
                try:
                    r = self.runner(["docker", "buildx", "version"], capture_output=True, text=True, timeout=30, check=False)
                    self._buildx_ok = r.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    self._buildx_ok = False
        return self._buildx_ok

    # ------------------------------------------------------------------ syntax
    def _buildx_check(self, repo_dir: Path, rel: str) -> tuple[Optional[int], str]:
        """Returns (returncode|None if unavailable, combined output)."""
        df = repo_dir / rel
        try:
            r = self._run(["docker", "buildx", "build", "--check", "-f", str(df), str(df.parent)], repo_dir, 180)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"{type(exc).__name__}: {exc}"
        out = redact((r.stdout or "") + (r.stderr or ""))
        if _DAEMON_DOWN_RE.search(out):
            return None, out
        return r.returncode, out

    def baseline(self, repo_dir: Path, rel: str) -> Optional[tuple[int, set[str]]]:
        """Run the buildx check on the ORIGINAL file so pre-existing lint warnings are not blamed on us."""
        if not self.cfg.get("buildx_check") or not self.buildx_available():
            return None
        rc, out = self._buildx_check(repo_dir, rel)
        if rc is None:
            return None
        return rc, _norm_lines(out)

    def validate_syntax(self, repo_dir: Path, rel: str, text: str, baseline=None) -> ValidationResult:
        res = ValidationResult()
        if self.cfg.get("buildx_check") and self.buildx_available():
            rc, out = self._buildx_check(repo_dir, rel)
            if rc is not None:
                if rc == 0:
                    return ValidationResult(PASSED, "buildx --check passed", ["docker buildx build --check: OK"])
                new = _norm_lines(out) - (baseline[1] if baseline else set())
                if baseline and baseline[0] != 0 and not new:
                    return ValidationResult(PASSED, "buildx --check: only pre-existing warnings",
                                            ["docker buildx build --check reported only warnings that already existed"])
                return ValidationResult(FAILED_V, "buildx --check failed",
                                        ["docker buildx build --check failed: " + "; ".join(sorted(new or _norm_lines(out)))[:600]])
            res.details.append("buildx unavailable (no Docker daemon/buildx) - used built-in static syntax check")
        else:
            res.details.append("buildx --check not used - built-in static syntax check")
        problems = static_check(text)
        if problems:
            res.status, res.summary = FAILED_V, "static syntax check failed"
            res.details += problems
        else:
            res.status, res.summary = PASSED, "static syntax check passed"
            res.details.append("static syntax check: OK (weaker than buildx --check)")
        return res

    # ------------------------------------------------------------------ build
    def validate_build(self, repo_dir: Path, rel: str) -> ValidationResult:
        if not self.cfg.get("docker_build"):
            return ValidationResult()
        if not self.buildx_available():
            return ValidationResult(NOT_EXECUTED, "docker build not executed", [],
                                    ["docker build requested but Docker is unavailable"])
        df = repo_dir / rel
        tag = "diu-validate:" + re.sub(r"[^a-z0-9]", "", rel.lower())[:40]
        try:
            r = self._run(["docker", "build", "-f", str(df), "-t", tag, str(df.parent)], repo_dir,
                          int(self.cfg.get("docker_build_timeout_seconds", 1800)))
        except subprocess.TimeoutExpired:
            return ValidationResult(FAILED_V, "docker build timed out", ["docker build timed out"])
        except (OSError, subprocess.SubprocessError) as exc:
            return ValidationResult(NOT_EXECUTED, "docker build not executed", [], [f"docker build could not start: {exc}"])
        finally:
            try:
                self.runner(["docker", "image", "rm", "-f", tag], capture_output=True, text=True, timeout=60, check=False)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        out = redact((r.stdout or "") + (r.stderr or ""))
        if r.returncode == 0:
            return ValidationResult(PASSED, "docker build passed", ["docker build: OK"])
        if _DAEMON_DOWN_RE.search(out):
            return ValidationResult(NOT_EXECUTED, "docker build not executed", [], ["Docker daemon not reachable"])
        return ValidationResult(FAILED_V, "docker build failed", ["docker build failed: " + out[-500:].strip()])

    # ------------------------------------------------------------------ technology validation
    def validate_technology(self, repo_dir: Path, techs: list[TechFinding]) -> ValidationResult:
        tcfg = self.cfg.get("technology", {})
        if not tcfg.get("enabled"):
            return ValidationResult()
        res = ValidationResult()
        ran_any = False
        done: set[tuple[str, str]] = set()
        for t in techs:
            cmds = tcfg.get("commands", {}).get(t.name)
            if not cmds or (t.name, t.directory) in done:
                continue
            done.add((t.name, t.directory))
            workdir = repo_dir if t.directory == "." else repo_dir / t.directory
            for cmd in cmds:
                argv = shlex.split(cmd)
                if not argv:
                    continue
                exe = argv[0]
                local = exe.startswith("./") and (workdir / exe).exists()
                if not local and not self.which(exe):
                    res.merge(ValidationResult(NOT_EXECUTED, "", [], [f"{t.name}: '{exe}' not installed on runner"]))
                    break
                try:
                    r = self._run(argv, workdir, int(tcfg.get("timeout_seconds", 900)))
                except subprocess.TimeoutExpired:
                    res.merge(ValidationResult(FAILED_V, "", [f"{t.name}: '{cmd}' timed out"]))
                    break
                except (OSError, subprocess.SubprocessError) as exc:
                    res.merge(ValidationResult(NOT_EXECUTED, "", [], [f"{t.name}: could not run '{cmd}': {exc}"]))
                    break
                ran_any = True
                tail = redact(((r.stdout or "") + (r.stderr or ""))[-300:]).strip()
                if r.returncode != 0:
                    if re.search(r"401|403|Unauthorized|private|credential|registry|ECONN|ENOTFOUND|network", tail, re.I):
                        res.merge(ValidationResult(NOT_EXECUTED, "", [], [f"{t.name}: '{cmd}' failed, likely missing credentials/private feed: {tail[:150]}"]))
                    else:
                        res.merge(ValidationResult(FAILED_V, "", [f"{t.name}: '{cmd}' exit {r.returncode}: {tail[:200]}"]))
                    break
                res.merge(ValidationResult(PASSED, "", [f"{t.name}: '{cmd}' OK"]))
        if not ran_any and not res.not_executed_reasons and res.status == NOT_RUN:
            res.details.append("technology validation enabled but no applicable technology/commands")
        res.summary = "technology validation " + res.status.lower()
        return res

    # ------------------------------------------------------------------ combined
    def validate_dockerfile(self, repo_dir: Path, rel: str, text: str, techs: list[TechFinding],
                            baseline=None) -> ValidationResult:
        total = self.validate_syntax(repo_dir, rel, text, baseline)
        if total.status == FAILED_V:
            return total
        total.merge(self.validate_build(repo_dir, rel))
        if total.status != FAILED_V:
            total.merge(self.validate_technology(repo_dir, techs))
        total.summary = {PASSED: "validation passed", FAILED_V: "validation failed",
                         NOT_EXECUTED: "syntax passed; some validation not executed"}.get(total.status, "")
        return total


def _norm_lines(out: str) -> set[str]:
    return {re.sub(r"\s+", " ", re.sub(r"\d+", "#", ln)).strip() for ln in out.splitlines()
            if ln.strip() and not ln.strip().startswith("#")}
