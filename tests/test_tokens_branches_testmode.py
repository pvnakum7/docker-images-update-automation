"""Custom Dockerfile names, multiple GitHub tokens, branch priority and TEST MODE (real local git repos, no network)."""
import subprocess

import pytest
import yaml
from openpyxl import Workbook

import src.main as main
from src.dockerfile_scanner import find_dockerfiles
from src.github_manager import (GitAccessError, GitHubManager, RepoInput, TokenPool, discover_token_names,
                                load_repositories)
from tests.helpers import default_registry, write
from tests.test_e2e_main import MULTI, git, make_repo, read_csv, settings_file
from tests.test_reports_email_config import FakeSMTP
import src.email_sender as es

TOK = {"GIT_TOKEN": "tok-one-AAAA1111", "GIT_TOKEN2": "tok-two-BBBB2222", "GIT_TOKEN3": "tok-three-CCCC3333"}


# ------------------------------------------------------------------ Dockerfile names
def test_custom_dockerfile_names_from_inventory_system_repo(tmp_path):
    for rel in ["Dockerfile", "frontend/Dockerfile", "frontend/Dockerfile.dev", "backend/Dockerfile.stage",
                "backend/Dockerfile.stage.bak", "README.md"]:
        write(tmp_path / rel, "FROM scratch\n")
    assert find_dockerfiles(tmp_path) == ["Dockerfile", "backend/Dockerfile.stage", "frontend/Dockerfile", "frontend/Dockerfile.dev"]


# ------------------------------------------------------------------ token discovery / pool
def test_discover_token_names_sequential_and_ordered():
    env = {"GIT_TOKEN3": "c", "GIT_TOKEN": "a", "GIT_TOKEN2": "b", "GIT_TOKEN10": "j", "GIT_TOKEN4": "  ",
           "SMTP_PASSWORD": "x", "GITHUB_TOKEN": "legacy"}
    assert discover_token_names(env) == ["GIT_TOKEN", "GIT_TOKEN2", "GIT_TOKEN3", "GIT_TOKEN10", "GITHUB_TOKEN"]


def test_pool_named_token_and_validation():
    pool = TokenPool({**TOK, "SMTP_PASSWORD": "secret"})
    (name, mgr), = pool.candidates("git_token2")
    assert name == "GIT_TOKEN2" and mgr.token == TOK["GIT_TOKEN2"]
    for bad in ["SMTP_PASSWORD", "PATH", "GIT_TOKEN_X", "../x"]:
        with pytest.raises(GitAccessError, match="invalid Token_Name"):
            pool.candidates(bad)                       # a sheet can never read an unrelated secret
    with pytest.raises(GitAccessError, match="GIT_TOKEN9 is not configured"):
        pool.candidates("GIT_TOKEN9")


def test_pool_without_name_tries_all_and_without_tokens_is_anonymous():
    assert [n for n, _ in TokenPool(TOK).candidates("")] == ["GIT_TOKEN", "GIT_TOKEN2", "GIT_TOKEN3"]
    assert [n for n, m in TokenPool({}).candidates("")] == ["(none)"] and TokenPool({}).candidates("")[0][1].token is None
    # the same value under two names is only used once
    assert [n for n, _ in TokenPool({"GIT_TOKEN": "same", "GITHUB_TOKEN": "same"}).candidates("")] == ["GIT_TOKEN"]


def test_token_value_goes_into_git_env_for_that_token_only(tmp_path):
    calls = []
    def runner(cmd, **kw):
        calls.append(kw["env"]); return subprocess.CompletedProcess(cmd, 0, "", "")
    pool = TokenPool(TOK, runner=runner)
    pool.candidates("GIT_TOKEN3")[0][1].clone("o/r", tmp_path / "d")
    import base64
    want = base64.b64encode(f"x-access-token:{TOK['GIT_TOKEN3']}".encode()).decode()
    vals = [v for k, v in calls[0].items() if k.startswith("GIT_CONFIG_VALUE_") and v.startswith("AUTHORIZATION")]
    assert vals == [f"AUTHORIZATION: basic {want}"]


def test_excel_token_name_column_is_read(tmp_path):
    wb = Workbook(); ws = wb.active
    ws.append(["Repo_Name", "Repo_Link", "POC", "Email", "CC_Email", "Token_Name"])
    ws.append(["a", "o/a", "p", "a@x.io", "", "GIT_TOKEN2"])
    ws.append(["b", "o/b", "p", "b@x.io", "", None])
    wb.save(tmp_path / "i.xlsx")
    rows = load_repositories(tmp_path / "i.xlsx")
    assert [r.token_name for r in rows] == ["GIT_TOKEN2", ""]


def test_sheet_without_token_column_still_loads(tmp_path):
    wb = Workbook(); ws = wb.active
    ws.append(["Repo_Name", "Repo_Link", "POC", "Email", "CC_Email"]); ws.append(["a", "o/a", "p", "a@x.io", ""])
    wb.save(tmp_path / "i.xlsx")
    assert load_repositories(tmp_path / "i.xlsx")[0].token_name == ""


# ------------------------------------------------------------------ branch selection
def lsremote(*branches):
    out = "".join(f"{'a' * 40}\trefs/heads/{b}\n" for b in branches)
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, "")


PRIO = ["dev", "development", "develop", "main"]


@pytest.mark.parametrize("present,expected", [
    (["main", "development", "dev"], "dev"),                  # dev wins over everything
    (["main", "development"], "development"),                 # then development
    (["main", "develop"], "develop"),
    (["main"], "main"),                                       # then main
    (["feature/x"], None),                                    # nothing -> repository default
    ([], None)])
def test_branch_priority(present, expected):
    assert GitHubManager(None, runner=lsremote(*present)).select_branch("o/r", PRIO) == expected


def test_clone_uses_selected_branch(tmp_path):
    seen = []
    GitHubManager(None, runner=lambda c, **k: seen.append(c) or subprocess.CompletedProcess(c, 0, "", "")).clone("o/r", tmp_path / "d", "dev")
    assert seen[0][:7] == ["git", "clone", "--depth", "1", "--quiet", "--branch", "dev"]


def test_ls_remote_auth_error_is_classified():
    gm = GitHubManager("t", runner=lambda c, **k: subprocess.CompletedProcess(c, 128, "", "fatal: Authentication failed"))
    with pytest.raises(GitAccessError) as e:
        gm.select_branch("o/r", PRIO)
    assert e.value.kind == "auth"


# ------------------------------------------------------------------ end-to-end (real git, mocked registry/SMTP)
def make_world(tmp_path, monkeypatch, rows):
    wb = Workbook(); ws = wb.active
    ws.append(["Repo_Name", "Repo_Link", "POC", "Email", "CC_Email", "Token_Name"])
    for r in rows:
        ws.append(r)
    inv = tmp_path / "repos.xlsx"; wb.save(inv)
    monkeypatch.setattr(main, "RegistryClient", lambda **kw: default_registry())
    FakeSMTP.instances.clear()
    monkeypatch.setattr(es.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(es.smtplib, "SMTP_SSL", FakeSMTP)
    return inv


def run(tmp_path, inv, env=None, **over):
    out = tmp_path / "out"
    e = {"SMTP_SERVER": "smtp.test", "SMTP_PORT": "587", "EMAIL_FROM": "bot@test.io", **TOK, **(env or {})}
    rc = main.run(["--config", str(settings_file(tmp_path, **over)), "--repos-file", str(inv), "--output-dir", str(out)], env=e)
    return rc, out


def branchy_repo(path, branches, files=None):
    r = make_repo(path, files or {"Dockerfile": MULTI})
    for b in branches:
        git(r, "branch", b)
    return r


def test_e2e_branch_priority_and_token_per_repo(tmp_path, monkeypatch):
    r1 = branchy_repo(tmp_path / "s/r1", ["development", "dev"], {"Dockerfile": "FROM python:3.12.5-slim\n"})
    git(r1, "checkout", "-q", "dev"); write(r1 / "Dockerfile", "FROM python:3.12.5-slim\nFROM nginx:1.27-alpine\n")
    git(r1, "commit", "-qam", "dev change"); git(r1, "checkout", "-q", "main")
    r2 = branchy_repo(tmp_path / "s/r2", ["development"])
    r3 = make_repo(tmp_path / "s/r3", {"Dockerfile": "FROM scratch\n"})              # only main
    inv = make_world(tmp_path, monkeypatch, [["r1", str(r1), "p", "o@x.io", "", "GIT_TOKEN2"],
                                             ["r2", str(r2), "p", "o@x.io", "", ""],
                                             ["r3", str(r3), "p", "o@x.io", "", "GIT_TOKEN3"]])
    rc, out = run(tmp_path, inv)
    rep = lambda n: next((tmp_path / "dockerfile-reports").glob(f"{n}-*.txt")).read_text()
    assert "Branch Analysed: dev" in rep("r1") and "GitHub Token Used: GIT_TOKEN2" in rep("r1")
    assert "Branch Analysed: development" in rep("r2") and "GitHub Token Used: GIT_TOKEN" in rep("r2")
    assert "Branch Analysed: main" in rep("r3") and "GitHub Token Used: GIT_TOKEN3" in rep("r3")
    rows = {r["Repo_Name"]: r for r in read_csv(out)}
    assert rows["r1"]["Total_Dockerfiles"] == "1" and rows["r1"]["Total_FROM_Instructions"] == "2"   # the DEV branch content was scanned
    all_text = "".join(p.read_text() for p in out.rglob("*") if p.is_file() and p.suffix in {".txt", ".csv", ".log"})
    assert not any(v in all_text for v in TOK.values())                                            # token values never leak


def test_e2e_unknown_token_name_fails_only_that_repo(tmp_path, monkeypatch):
    r1 = make_repo(tmp_path / "s/r1", {"Dockerfile": "FROM scratch\n"})
    inv = make_world(tmp_path, monkeypatch, [["bad", str(r1), "p", "o@x.io", "", "GIT_TOKEN9"],
                                             ["ok", str(r1), "p", "o@x.io", "", "GIT_TOKEN"]])
    rc, out = run(tmp_path, inv)
    rows = {r["Repo_Name"]: r for r in read_csv(out)}
    assert "GIT_TOKEN9 is not configured" in rows["bad"]["Error"] and rows["ok"]["Error"] == ""


def test_e2e_no_name_tries_next_token_on_auth_failure(tmp_path, monkeypatch):
    r1 = make_repo(tmp_path / "s/r1", {"Dockerfile": "FROM scratch\n"})
    inv = make_world(tmp_path, monkeypatch, [["r1", str(r1), "p", "o@x.io", "", ""]])
    real = main.GitHubManager.select_branch
    def picky(self, link, prio):
        if self.token != TOK["GIT_TOKEN3"]:
            raise GitAccessError("not_found", "repository not found (or token has no access)")
        return real(self, link, prio)
    monkeypatch.setattr(main.GitHubManager, "select_branch", picky)
    rc, out = run(tmp_path, inv)
    assert "GitHub Token Used: GIT_TOKEN3" in next((tmp_path / "dockerfile-reports").glob("r1-*.txt")).read_text()


def test_e2e_all_tokens_denied_reports_error(tmp_path, monkeypatch):
    r1 = make_repo(tmp_path / "s/r1", {"Dockerfile": "FROM scratch\n"})
    inv = make_world(tmp_path, monkeypatch, [["r1", str(r1), "p", "o@x.io", "", ""]])
    def deny(self, link, prio):
        raise GitAccessError("permission", "permission denied - token lacks access to this repository")
    monkeypatch.setattr(main.GitHubManager, "select_branch", deny)
    rc, out = run(tmp_path, inv)
    assert read_csv(out)[0]["Error"].startswith("permission")


def test_e2e_pr_base_is_the_selected_branch_and_commit_uses_same_token(tmp_path, monkeypatch):
    r1 = branchy_repo(tmp_path / "s/r1", ["dev"], {"Dockerfile": "FROM python:3.12.5-slim\n"})
    inv = make_world(tmp_path, monkeypatch, [["r1", str(r1), "p", "o@x.io", "", "GIT_TOKEN2"]])
    prs = []
    monkeypatch.setattr(main.GitHubManager, "create_pull_request",
                        lambda self, link, head, base, title, body: prs.append((self.token, base)) or "https://pr/1")
    rc, out = run(tmp_path, inv, env={"DIU_TEST_MODE": "false"}, upgrade={"report_only": False},
                  git={"commit_changes": True, "create_pull_requests": True})
    assert prs == [(TOK["GIT_TOKEN2"], "dev")]


# ------------------------------------------------------------------ TEST MODE
def test_test_mode_sends_no_email_and_creates_no_pr_but_does_everything_else(tmp_path, monkeypatch):
    r1 = make_repo(tmp_path / "s/r1", {"Dockerfile": "FROM python:3.12.5-slim\n"})
    inv = make_world(tmp_path, monkeypatch, [["r1", str(r1), "p", "o@x.io", "d@x.io", ""]])
    prs = []
    monkeypatch.setattr(main.GitHubManager, "create_pull_request", lambda *a, **k: prs.append(1) or "x")
    rc, out = run(tmp_path, inv, env={"DIU_TEST_MODE": "true", "DIU_REPORT_ONLY": "false", "DIU_CREATE_PR": "true"})
    assert rc == 0 and prs == [] and FakeSMTP.instances == []                       # nothing sent, no PR
    assert list(out.glob("Dockerfile-update-report-*.csv")) and list(out.glob("Docker-repository-reports-*.zip"))
    assert (out / "email-recipient-summary.txt").exists() and (out / "email-preview-NOT-SENT.eml").exists()
    assert "TEST MODE - email NOT sent" in (out / "overall-summary.txt").read_text()
    t = next((tmp_path / "dockerfile-reports").glob("r1-*.txt")).read_text()
    assert "TEST MODE: pull request NOT created" in t and "Branch Analysed: main" in t
    assert read_csv(out)[0]["Updated_Images"] == "1"                                 # analysis + edit still happened


def test_test_mode_off_sends_email(tmp_path, monkeypatch):
    r1 = make_repo(tmp_path / "s/r1", {"Dockerfile": "FROM scratch\n"})
    inv = make_world(tmp_path, monkeypatch, [["r1", str(r1), "p", "o@x.io", "", ""]])
    rc, out = run(tmp_path, inv, env={"DIU_TEST_MODE": "false"})
    assert len(FakeSMTP.instances) == 1 and FakeSMTP.instances[0].sent


def test_config_branch_priority_validation_and_base_override():
    from src.config import load_settings
    from tests.test_recipients import ROOT
    s = load_settings(ROOT / "config/settings.yaml", {})
    assert s["git"]["branch_priority"] == ["dev", "development", "develop", "main"] and s["testing"]["enabled"] is False
    assert load_settings(ROOT / "config/settings.yaml", {"DIU_BRANCH_PRIORITY": "release, main"})["git"]["branch_priority"] == ["release", "main"]
    with pytest.raises(ValueError, match="invalid branch name"):
        load_settings(ROOT / "config/settings.yaml", {"DIU_BRANCH_PRIORITY": "--upload-pack=x"})
