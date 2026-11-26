"""Repository inventory (Excel), cloning with token auth, commit/push and pull-request creation."""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
from openpyxl import load_workbook

from .logger import get_logger, register_secret, redact

log = get_logger("github")

REQUIRED_COLUMNS = ["Repo_Name", "Repo_Link", "POC", "Email", "CC_Email"]


class GitAccessError(Exception):
    """kind: auth | not_found | permission | rate_limit | network | invalid | git"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class RepoInput:
    repo_name: str
    repo_link: str
    poc: str = ""
    email: str = ""
    cc_email: str = ""
    row_error: Optional[str] = None
    token_name: str = ""          # optional Excel column `Token_Name`, e.g. GIT_TOKEN2 (empty = try every configured token)


def load_repositories(path: str | Path) -> list[RepoInput]:
    """Read the Excel inventory. Every data row becomes a RepoInput (bad rows carry row_error)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"repository inventory not found: {p}")
    wb = load_workbook(p, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    try:
        header = [str(c).strip() if c is not None else "" for c in next(rows)]
    except StopIteration as exc:
        raise ValueError("repository inventory is empty") from exc
    idx = {h.lower(): i for i, h in enumerate(header)}
    missing = [c for c in REQUIRED_COLUMNS if c.lower() not in idx]
    if missing:
        raise ValueError(f"inventory is missing required column(s): {', '.join(missing)}")

    def cell(r, name):
        i = idx[name.lower()]
        v = r[i] if i < len(r) else None
        return "" if v is None else str(v).strip()

    out: list[RepoInput] = []
    for r in rows:
        if r is None or all(c is None or str(c).strip() == "" for c in r):
            continue
        ri = RepoInput(cell(r, "Repo_Name"), cell(r, "Repo_Link"), cell(r, "POC"), cell(r, "Email"), cell(r, "CC_Email"))
        if "token_name" in idx:
            ri.token_name = cell(r, "Token_Name")
        if not ri.repo_link:
            ri.row_error = "Repo_Link is empty"
        if not ri.repo_name:
            ri.repo_name = _name_from_link(ri.repo_link) or f"row-{len(out) + 2}"
        out.append(ri)
    wb.close()
    return out


# ---------------------------------------------------------------------- multiple tokens
# Only these environment-variable names may be referenced from the sheet, so a sheet can never be used
# to read an unrelated secret such as SMTP_PASSWORD.
_TOKEN_NAME_RE = re.compile(r"^GIT_TOKEN(\d*)$")
_LEGACY_TOKEN_NAMES = ("GITHUB_TOKEN", "GH_TOKEN", "GH_PAT")


def is_token_name(name: str) -> bool:
    return bool(_TOKEN_NAME_RE.match(name)) or name in _LEGACY_TOKEN_NAMES


def _token_sort_key(name: str):
    m = _TOKEN_NAME_RE.match(name)
    return (0, int(m.group(1) or 1)) if m else (1, 0)      # GIT_TOKEN, GIT_TOKEN2, GIT_TOKEN3 ... then legacy names


def discover_token_names(env) -> list[str]:
    """Names of all non-empty token variables: GIT_TOKEN, GIT_TOKEN2, ... (then GITHUB_TOKEN / GH_TOKEN / GH_PAT)."""
    names = [k for k, v in env.items() if is_token_name(k) and (v or "").strip()]
    return sorted(names, key=_token_sort_key)


class TokenPool:
    """Several GitHub tokens, selected per repository by name (never by value).

    * the sheet's `Token_Name` cell names the token for that repository (e.g. GIT_TOKEN2);
    * an empty cell means "try every configured token in order until one has access";
    * the chosen token is used for the whole chain: branch lookup, clone, push and pull request.
    """

    def __init__(self, env, **manager_kwargs):
        self._env, self._kw = env, manager_kwargs
        self._cache: dict[str, "GitHubManager"] = {}
        self.names = discover_token_names(env)
        seen: dict[str, str] = {}
        for n in list(self.names):                      # same value under two names (GITHUB_TOKEN == GIT_TOKEN) counts once
            v = env[n].strip()
            if v in seen:
                self.names.remove(n)
            else:
                seen[v] = n
            register_secret(v)

    def manager(self, name: str) -> "GitHubManager":
        if name not in self._cache:
            self._cache[name] = GitHubManager(self._env[name].strip(), **self._kw)
        return self._cache[name]

    def candidates(self, token_name: str = "") -> list[tuple[str, "GitHubManager"]]:
        token_name = (token_name or "").strip().upper()
        if token_name:
            if not is_token_name(token_name):
                raise GitAccessError("auth", f"invalid Token_Name {token_name!r} (expected GIT_TOKEN, GIT_TOKEN2, GIT_TOKEN3 ...)")
            if not (self._env.get(token_name) or "").strip():
                raise GitAccessError("auth", f"token {token_name} is not configured (secret/environment variable missing or empty)")
            return [(token_name, self.manager(token_name))]
        if self.names:
            return [(n, self.manager(n)) for n in self.names]
        if "(none)" not in self._cache:
            self._cache["(none)"] = GitHubManager(None, **self._kw)
        return [("(none)", self._cache["(none)"])]


def _name_from_link(link: str) -> str:
    m = re.search(r"([^/:]+?)(?:\.git)?/?$", link or "")
    return m.group(1) if m else ""


def parse_repo_url(link: str) -> tuple[str, str, str]:
    """Return (host, owner, repo). Accepts https URLs, git@host:o/r.git and 'owner/repo'."""
    link = (link or "").strip()
    m = re.match(r"^git@([^:]+):([^/]+)/(.+?)(?:\.git)?/?$", link)
    if m:
        return m.group(1), m.group(2), m.group(3)
    if re.match(r"^[\w.\-]+/[\w.\-]+$", link):
        owner, repo = link.split("/")
        return "github.com", owner, repo.removesuffix(".git")
    u = urlparse(link)
    if u.scheme in {"http", "https"} and u.hostname:
        parts = [x for x in u.path.split("/") if x]
        if len(parts) >= 2:
            return u.hostname, parts[0], parts[1].removesuffix(".git")
    raise GitAccessError("invalid", f"cannot parse repository link: {link!r}")


def _is_local(link: str) -> bool:
    """Local paths / file:// URLs are supported so the pipeline can be tested without network or tokens."""
    return link.startswith("file://") or (os.path.isabs(link) and os.path.isdir(link))


class GitHubManager:
    def __init__(self, token: Optional[str], runner: Callable = subprocess.run, retries: int = 3,
                 backoff: float = 2.0, sleep: Callable[[float], None] = time.sleep,
                 session: Optional[requests.Session] = None):
        self.token = token or None
        register_secret(self.token)
        if self.token:
            register_secret(base64.b64encode(f"x-access-token:{self.token}".encode()).decode())
        self.runner, self.retries, self.backoff, self.sleep = runner, retries, backoff, sleep
        self.session = session or requests.Session()

    # ------------------------------------------------------------------ git plumbing
    def _env(self, host: str) -> dict:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        env.pop("GITHUB_TOKEN", None)
        if self.token:
            b64 = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            # passed via env (not argv) so it never shows in process listings or logs.
            # Append after any GIT_CONFIG_* entries already present instead of clobbering them.
            try:
                n = int(env.get("GIT_CONFIG_COUNT", "0"))
            except ValueError:
                n = 0
            env.update({"GIT_CONFIG_COUNT": str(n + 1),
                        f"GIT_CONFIG_KEY_{n}": f"http.https://{host}/.extraheader",
                        f"GIT_CONFIG_VALUE_{n}": f"AUTHORIZATION: basic {b64}"})
        return env

    def _git(self, args: list[str], cwd: Optional[Path], host: str = "github.com", timeout: int = 900):
        return self.runner(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                           timeout=timeout, env=self._env(host), check=False)

    @staticmethod
    def classify_git_error(stderr: str) -> GitAccessError:
        s = redact(stderr or "").strip()
        low = s.lower()
        if "rate limit" in low or "429" in low:
            return GitAccessError("rate_limit", "GitHub rate limit hit while cloning")
        if "authentication failed" in low or "invalid username or password" in low or "bad credentials" in low or "401" in low:
            return GitAccessError("auth", "authentication failed - token invalid/expired or missing")
        if ("repository not found" in low or ("not found" in low and "repository" in low) or "404" in low
                or "does not appear to be a git repository" in low):
            return GitAccessError("not_found", "repository not found (or token has no access to this private repository)")
        if "permission denied" in low or "403" in low or "access denied" in low:
            return GitAccessError("permission", "permission denied - token lacks access to this repository")
        if any(k in low for k in ("could not resolve host", "connection timed out", "connection refused",
                                  "unable to access", "network is unreachable", "early eof", "ssl", "timed out")):
            return GitAccessError("network", "network error while contacting GitHub")
        return GitAccessError("git", f"git error: {s[:200]}")

    def select_branch(self, link: str, priority: list[str]) -> Optional[str]:
        """First branch of `priority` that exists on the remote (git ls-remote), else None (= repository default)."""
        if not priority:
            return None
        if _is_local(link):
            url, host = link, "localhost"
        else:
            host, owner, repo = parse_repo_url(link)
            url = f"https://{host}/{owner}/{repo}.git"
        last: Optional[GitAccessError] = None
        for attempt in range(self.retries + 1):
            try:
                r = self._git(["ls-remote", "--heads", url, *[f"refs/heads/{b}" for b in priority]], None, host, timeout=120)
            except subprocess.TimeoutExpired:
                last = GitAccessError("network", "git ls-remote timed out")
            except OSError as exc:
                raise GitAccessError("git", f"git executable problem: {exc}") from exc
            else:
                if r.returncode == 0:
                    present = {ln.split("\t")[-1].removeprefix("refs/heads/").strip() for ln in (r.stdout or "").splitlines() if "\t" in ln}
                    return next((b for b in priority if b in present), None)
                last = self.classify_git_error(r.stderr)
            if last.kind not in {"network", "rate_limit"} or attempt == self.retries:
                break
            self.sleep(self.backoff * (2 ** attempt))
        assert last is not None
        raise last

    def clone(self, link: str, dest: Path, branch: Optional[str] = None) -> Path:
        if _is_local(link):
            url, host = link, "localhost"
        else:
            host, owner, repo = parse_repo_url(link)
            url = f"https://{host}/{owner}/{repo}.git"
        last: Optional[GitAccessError] = None
        for attempt in range(self.retries + 1):
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            try:
                br = ["--branch", branch] if branch else []
                r = self._git(["clone", "--depth", "1", "--quiet", *br, url, str(dest)], None, host)
            except subprocess.TimeoutExpired:
                last = GitAccessError("network", "git clone timed out")
            except OSError as exc:
                raise GitAccessError("git", f"git executable problem: {exc}") from exc
            else:
                if r.returncode == 0:
                    return dest
                last = self.classify_git_error(r.stderr)
            log.warning("clone attempt %d/%d failed: %s", attempt + 1, self.retries + 1, last)
            if last.kind not in {"network", "rate_limit"} or attempt == self.retries:
                break
            self.sleep(self.backoff * (2 ** attempt))
        assert last is not None
        raise last

    # ------------------------------------------------------------------ commit / push / PR
    def default_branch(self, repo_dir: Path) -> str:
        r = self._git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
        return (r.stdout or "main").strip() or "main"

    def commit_and_push(self, repo_dir: Path, link: str, branch: str, files: list[str], message: str,
                        author_name: str, author_email: str) -> None:
        host = "localhost" if _is_local(link) else parse_repo_url(link)[0]
        steps = [["checkout", "-b", branch], ["add", "--", *files],
                 ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", message],
                 ["push", "origin", f"HEAD:refs/heads/{branch}"]]
        for st in steps:
            r = self._git(st, repo_dir, host)
            if r.returncode != 0:
                raise self.classify_git_error(r.stderr or r.stdout)

    def create_pull_request(self, link: str, head: str, base: str, title: str, body: str) -> str:
        host, owner, repo = parse_repo_url(link)
        api = "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
        if not self.token:
            raise GitAccessError("auth", "GITHUB_TOKEN required to create pull requests")
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        payload = {"title": title, "head": head, "base": base, "body": body}
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.post(f"{api}/repos/{owner}/{repo}/pulls", json=payload, headers=headers, timeout=30)
            except requests.exceptions.RequestException as exc:
                if attempt == self.retries:
                    raise GitAccessError("network", f"GitHub API unreachable: {type(exc).__name__}") from exc
                self.sleep(self.backoff * (2 ** attempt))
                continue
            if resp.status_code == 201:
                return resp.json().get("html_url", "")
            if resp.status_code in (403, 429) and ("rate limit" in resp.text.lower() or resp.headers.get("Retry-After")):
                if attempt == self.retries:
                    raise GitAccessError("rate_limit", "GitHub API rate limit exceeded")
                reset = resp.headers.get("X-RateLimit-Reset")
                wait = float(resp.headers.get("Retry-After") or (max(0, int(reset) - time.time()) if reset else self.backoff * (2 ** attempt)))
                self.sleep(min(wait, 60))
                continue
            if resp.status_code == 401:
                raise GitAccessError("auth", "GitHub API authentication failed")
            if resp.status_code == 403:
                raise GitAccessError("permission", "token lacks permission to open pull requests")
            if resp.status_code == 404:
                raise GitAccessError("not_found", "repository not found for pull request")
            if resp.status_code == 422:
                raise GitAccessError("git", "pull request rejected (already exists or no diff): " + resp.text[:150])
            raise GitAccessError("git", f"GitHub API returned HTTP {resp.status_code}")
        raise GitAccessError("network", "GitHub API unreachable")  # pragma: no cover


def _force_writable(func, path, _exc):
    """rmtree helper: git object files are read-only on Windows."""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


def clean_directory_contents(path: Path) -> int:
    """Delete everything INSIDE `path` (the directory itself stays). Symlinks are unlinked, never followed.
    Returns the number of top-level entries removed."""
    n = 0
    if not path.is_dir() or path.is_symlink():
        return 0
    for child in list(path.iterdir()):
        try:
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child, onerror=_force_writable)
            n += 1
        except OSError as exc:
            log.warning("could not remove %s: %s", child.name, exc)
    return n


def make_temp_dir(prefix: str = "diu-") -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.chmod(d, 0o700)
    return d
