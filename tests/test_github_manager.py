import subprocess

import pytest
import requests
from openpyxl import Workbook

from src.github_manager import GitAccessError, GitHubManager, load_repositories, parse_repo_url

TOKEN = "ghp_TESTTOKEN_abcdef123456"


def xlsx(path, rows, header=("Repo_Name", "Repo_Link", "POC", "Email", "CC_Email")):
    wb = Workbook(); ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    wb.save(path)
    return path


def test_parse_urls():
    assert parse_repo_url("https://github.com/org/app") == ("github.com", "org", "app")
    assert parse_repo_url("https://github.com/org/app.git") == ("github.com", "org", "app")
    assert parse_repo_url("https://github.com/org/app/tree/main/x") == ("github.com", "org", "app")
    assert parse_repo_url("git@github.com:org/app.git") == ("github.com", "org", "app")
    assert parse_repo_url("org/app") == ("github.com", "org", "app")
    assert parse_repo_url("https://ghe.corp.com/o/r") == ("ghe.corp.com", "o", "r")
    with pytest.raises(GitAccessError):
        parse_repo_url("not a url")


def test_load_repositories(tmp_path):
    p = xlsx(tmp_path / "r.xlsx", [("a", "https://github.com/o/a", "P1", "p1@x.io", "c@x.io"), (None, None, None, None, None),
                                   ("b", "", "P2", "p2@x.io", ""), ("", "https://github.com/o/c", "", "", "")])
    rows = load_repositories(p)
    assert [r.repo_name for r in rows] == ["a", "b", "c"]            # blank row skipped, name derived from link
    assert rows[0].poc == "P1" and rows[0].cc_email == "c@x.io"
    assert rows[1].row_error == "Repo_Link is empty" and rows[2].row_error is None


def test_load_repositories_header_case_and_missing_column(tmp_path):
    p = xlsx(tmp_path / "r.xlsx", [("a", "l", "p", "e", "c")], header=("repo_name", "REPO_LINK", "poc", "email", "cc_email"))
    assert len(load_repositories(p)) == 1
    bad = xlsx(tmp_path / "b.xlsx", [("a", "l")], header=("Repo_Name", "Repo_Link"))
    with pytest.raises(ValueError, match="missing required column"):
        load_repositories(bad)
    with pytest.raises(FileNotFoundError):
        load_repositories(tmp_path / "nope.xlsx")


@pytest.mark.parametrize("stderr,kind", [
    ("remote: Repository not found.\nfatal: repository 'https://github.com/o/x/' not found", "not_found"),
    ("fatal: Authentication failed for 'https://github.com/o/x/'", "auth"),
    ("remote: Permission to o/x.git denied to bot.\nfatal: unable to access: The requested URL returned error: 403", "permission"),
    ("fatal: unable to access 'https://github.com/o/x/': Could not resolve host: github.com", "network"),
    ("error: RPC failed; HTTP 429 rate limit exceeded", "rate_limit"),
    ("fatal: something weird", "git")])
def test_classify(stderr, kind):
    assert GitHubManager.classify_git_error(stderr).kind == kind


class Runner:
    def __init__(self, results):
        self.results, self.calls = list(results), []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return subprocess.CompletedProcess(cmd, r[0], "", r[1])


def test_clone_token_never_in_argv_and_passed_via_env(tmp_path):
    run = Runner([(0, "")])
    GitHubManager(TOKEN, runner=run).clone("https://github.com/o/r", tmp_path / "d")
    cmd, kw = run.calls[0]
    assert TOKEN not in " ".join(cmd) and cmd[:2] == ["git", "clone"] and cmd[-2] == "https://github.com/o/r.git"
    assert any(v.startswith("AUTHORIZATION: basic") for k, v in kw["env"].items() if k.startswith("GIT_CONFIG_VALUE_"))
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "GITHUB_TOKEN" not in kw["env"]


def test_clone_public_without_token_has_no_auth_header(tmp_path):
    run = Runner([(0, "")])
    GitHubManager(None, runner=run).clone("https://github.com/o/r", tmp_path / "d")
    assert not any(v.startswith("AUTHORIZATION") for k, v in run.calls[0][1]["env"].items() if k.startswith("GIT_CONFIG_VALUE_"))


def test_clone_retries_network_errors_with_backoff(tmp_path):
    sleeps = []
    run = Runner([(128, "fatal: Could not resolve host"), (128, "fatal: Could not resolve host"), (0, "")])
    GitHubManager(TOKEN, runner=run, sleep=sleeps.append, backoff=2).clone("o/r", tmp_path / "d")
    assert len(run.calls) == 3 and sleeps == [2, 4]


def test_clone_does_not_retry_auth_errors(tmp_path):
    run = Runner([(128, "fatal: Authentication failed")])
    with pytest.raises(GitAccessError) as e:
        GitHubManager(TOKEN, runner=run, sleep=lambda s: None).clone("o/r", tmp_path / "d")
    assert e.value.kind == "auth" and len(run.calls) == 1


def test_clone_timeout_is_network_error(tmp_path):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    with pytest.raises(GitAccessError) as e:
        GitHubManager(TOKEN, runner=boom, retries=1, sleep=lambda s: None).clone("o/r", tmp_path / "d")
    assert e.value.kind == "network"


class Resp:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code, self._b, self.headers, self.text = status, body or {}, headers or {}, text

    def json(self):
        return self._b


class Sess:
    def __init__(self, resps):
        self.resps, self.calls = list(resps), []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        r = self.resps.pop(0) if len(self.resps) > 1 else self.resps[0]
        if isinstance(r, Exception):
            raise r
        return r


def test_create_pr_success_and_errors():
    s = Sess([Resp(201, {"html_url": "https://github.com/o/r/pull/5"})])
    gm = GitHubManager(TOKEN, session=s, sleep=lambda x: None)
    assert gm.create_pull_request("https://github.com/o/r", "br", "main", "t", "b") == "https://github.com/o/r/pull/5"
    assert s.calls[0][0] == "https://api.github.com/repos/o/r/pulls" and s.calls[0][2]["Authorization"] == f"Bearer {TOKEN}"
    for status, kind in [(401, "auth"), (403, "permission"), (404, "not_found"), (422, "git")]:
        with pytest.raises(GitAccessError) as e:
            GitHubManager(TOKEN, session=Sess([Resp(status, text="x")]), sleep=lambda x: None).create_pull_request("o/r", "h", "b", "t", "b")
        assert e.value.kind == kind
    with pytest.raises(GitAccessError) as e:
        GitHubManager(None, session=s).create_pull_request("o/r", "h", "b", "t", "b")
    assert e.value.kind == "auth"


def test_create_pr_rate_limit_retry_then_ok():
    sleeps = []
    s = Sess([Resp(403, text="API rate limit exceeded", headers={"Retry-After": "3"}), Resp(201, {"html_url": "u"})])
    assert GitHubManager(TOKEN, session=s, sleep=sleeps.append).create_pull_request("o/r", "h", "b", "t", "b") == "u"
    assert sleeps == [3.0]


def test_create_pr_network_error():
    s = Sess([requests.exceptions.ConnectionError("x")])
    with pytest.raises(GitAccessError) as e:
        GitHubManager(TOKEN, session=s, retries=1, sleep=lambda x: None).create_pull_request("o/r", "h", "b", "t", "b")
    assert e.value.kind == "network"
