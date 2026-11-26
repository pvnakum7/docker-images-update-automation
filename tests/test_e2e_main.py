"""End-to-end runs of src.main.run() against REAL local git repositories (no network, no tokens).
Registry responses are mocked; SMTP is mocked. Proves orchestration, failure isolation, reports,
CSV, summary counts, e-mail attachments and the commit-to-branch path."""
import csv
from datetime import datetime, timezone
import io
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml
from openpyxl import Workbook

import src.main as main
from src.report_generator import format_report_date
from tests.helpers import default_registry, write
from tests.test_reports_email_config import FakeSMTP

MULTI = "FROM node:22.22.0-slim AS builder\nRUN npm ci\nFROM nginx:1.27-alpine AS production\nCOPY --from=builder /a /b\n"


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def make_repo(path: Path, files: dict) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for rel, c in files.items():
        write(path / rel, c)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    a = make_repo(tmp_path / "src-repos/repo-a", {"Dockerfile": MULTI, "api/Dockerfile": "FROM python:3.12.5-slim\n"})
    b = make_repo(tmp_path / "src-repos/repo-b", {"Dockerfile": "FROM scratch\nFROM unknown/img:1.0\n", "README.md": "x"})
    wb = Workbook(); ws = wb.active
    ws.append(["Repo_Name", "Repo_Link", "POC", "Email", "CC_Email"])
    ws.append(["repo-a", str(a), "Person-A", "a@example.com", "cc@example.com"])
    ws.append(["repo-b", str(b), "Person-B", "b@example.com", ""])
    ws.append(["repo-missing", "file://" + str(tmp_path / "does-not-exist"), "Person-C", "c@example.com", ""])
    ws.append(["repo-nolink", "", "Person-D", "d@example.com", ""])
    inv = tmp_path / "repos.xlsx"
    wb.save(inv)
    monkeypatch.setattr(main, "RegistryClient", lambda **kw: default_registry())
    FakeSMTP.instances.clear()
    import src.email_sender as es
    monkeypatch.setattr(es.smtplib, "SMTP", FakeSMTP)
    return dict(tmp=tmp_path, a=a, b=b, inv=inv)


def settings_file(tmp, **over):
    cfg = {"upgrade": {"strategy": "same_major", "report_only": True},
           "output": {"logs_dir": str(tmp / "out/logs"), "dockerfile_reports_dir": str(tmp / "dockerfile-reports"),
                      "clones_dir": str(tmp / "repos")},
           "registry": {"retry_count": 0}}
    for k, v in over.items():
        cfg.setdefault(k, {}).update(v)
    p = tmp / "settings.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


ENV = {"SMTP_SERVER": "smtp.test", "SMTP_PORT": "587", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "pw-secret-123",
       "EMAIL_FROM": "bot@test.io", "EMAIL_TO": "secret-team@test.io", "EMAIL_CC": "secret-cc@test.io"}


def run(world, extra_env=None, args=(), **over):
    tmp = world["tmp"]
    out = tmp / "out"
    env = {**ENV, **(extra_env or {})}
    rc = main.run(["--config", str(settings_file(tmp, **over)), "--repos-file", str(world["inv"]), "--output-dir", str(out), *args], env=env)
    return rc, out


def read_csv(out):
    f = next(out.glob("Dockerfile-update-report-*.csv"))
    return list(csv.DictReader(io.StringIO(f.read_text())))


def test_dry_run_reports_everything_and_modifies_nothing(world, capsys):
    head_before = git(world["a"], "rev-parse", "HEAD")
    rc, out = run(world)
    assert rc == 0
    rows = {r["Repo_Name"]: r for r in read_csv(out)}
    assert list(rows) == ["repo-a", "repo-b", "repo-missing", "repo-nolink"]            # ALL inventory rows present
    a = rows["repo-a"]
    assert (a["Total_Dockerfiles"], a["Total_FROM_Instructions"], a["Updated_Images"], a["Pending_Images"]) == ("2", "3", "0", "3")
    assert a["POC"] == "Person-A" and a["Email"] == "a@example.com" and a["CC_Email"] == "cc@example.com"
    assert rows["repo-b"]["Skipped_Images"] == "2" and rows["repo-b"]["Skipped_Dockerfiles"] == "1"
    assert rows["repo-missing"]["Overall_Status"] == "FAILED" and rows["repo-missing"]["Error"].startswith("not_found")
    assert rows["repo-nolink"]["Overall_Status"] == "FAILED" and "Repo_Link is empty" in rows["repo-nolink"]["Error"]
    # per-repo text reports for EVERY repo (including failed ones)
    central = world["tmp"] / "dockerfile-reports"
    d = format_report_date(datetime.now(timezone.utc))
    reps = sorted(p.name for p in central.glob("*.txt"))
    assert reps == [f"repo-a-{d}.txt", f"repo-b-{d}.txt", f"repo-missing-{d}.txt", f"repo-nolink-{d}.txt"]
    assert not (out / "repository-reports").exists()
    for repo in ("a", "b"):                                      # nothing is ever written into a repository
        assert not list(world[repo].glob("*-Docker-report*")) and not list(world[repo].glob("*-2*.txt"))
    assert "Centralized reports: 4 of 4 available" in (out / "overall-summary.txt").read_text()
    ta = (central / f"repo-a-{d}.txt").read_text()
    assert "node:22.22.0-slim" in ta and "node:22.24.0-slim" in ta and "UPDATE_AVAILABLE" in ta and "Total FROM instructions: 3" in ta
    # source repository untouched
    assert git(world["a"], "rev-parse", "HEAD") == head_before and git(world["a"], "status", "--porcelain") == ""
    assert (world["a"] / "Dockerfile").read_text() == MULTI
    # overall summary in logs/stdout
    stdout = capsys.readouterr().out
    assert "Overall Dockerfile Update Report" in stdout and "Total Repositories: 4" in stdout
    assert "Successfully Processed Repositories: 2" in stdout and "Failed Repositories: 2" in stdout
    assert "Total Dockerfiles: 3" in stdout and "Total FROM Instructions: 5" in stdout and "pw-secret-123" not in stdout


def test_email_has_summary_attachments_and_all_recipients(world):
    rc, out = run(world)
    srv = FakeSMTP.instances[0]
    msg, frm, to = srv.sent[0]
    assert srv.tls and srv.login_args == ("u", "pw-secret-123") and frm == "bot@test.io"
    # recipients come from the Excel rows (incl. repos that failed); the EMAIL_TO/EMAIL_CC secrets are ignored by default
    assert set(to) == {"a@example.com", "b@example.com", "c@example.com", "d@example.com", "cc@example.com"}
    assert msg["To"] == "a@example.com, b@example.com, c@example.com, d@example.com" and msg["Cc"] == "cc@example.com"
    assert len(FakeSMTP.instances) == 1 and len(FakeSMTP.instances[0].sent) == 1      # exactly ONE consolidated email
    assert not any("secret-" in a for a in to)
    summ = (world["tmp"] / "out/email-recipient-summary.txt").read_text()
    assert "Total unique recipients: 4" in summ and "Total unique recipients: 1" in summ
    atts = {a.get_filename(): a for a in msg.iter_attachments()}
    csv_name = next(n for n in atts if n.endswith(".csv"))
    assert csv_name.startswith("Dockerfile-update-report-")
    zname = next(n for n in atts if n.endswith(".zip"))
    z = zipfile.ZipFile(io.BytesIO(atts[zname].get_payload(decode=True)))
    assert len(z.namelist()) == 4 and any(n.startswith("repo-a-") for n in z.namelist())
    body = msg.get_body().get_content()
    assert "Total Repositories: 4" in body and "Total Dockerfiles: 3" in body and "Status: PARTIAL_SUCCESS" in body
    assert msg["Subject"].startswith("Docker Image Upgrade Automation Report - 20")
    assert "pw-secret-123" not in msg.as_string()


def test_email_failure_does_not_fail_run(world, monkeypatch):
    import src.email_sender as es
    def boom(*a, **k):
        raise OSError("smtp down")
    monkeypatch.setattr(es.smtplib, "SMTP", boom)
    rc, out = run(world)
    assert rc == 0 and "SMTP error" in (out / "overall-summary.txt").read_text()


def test_update_mode_commits_to_new_branch_only(world):
    rc, out = run(world, upgrade={"report_only": False}, git={"commit_changes": True})
    assert rc == 0
    rows = {r["Repo_Name"]: r for r in read_csv(out)}
    a = rows["repo-a"]
    assert (a["Updated_Dockerfiles"], a["Updated_Images"], a["Skipped_Images"], a["Failed_Images"]) == ("2", "3", "0", "0")
    assert a["Overall_Status"] == "SUCCESS"
    branches = [b.strip() for b in git(world["a"], "branch", "--list", "docker-image-update/*").splitlines()]
    assert len(branches) == 1
    br = branches[0].lstrip("* ").strip()
    new = git(world["a"], "show", f"{br}:Dockerfile")
    assert new == MULTI.replace("22.22.0", "22.24.0").replace("1.27-alpine", "1.29.0-alpine")
    assert git(world["a"], "show", f"{br}:api/Dockerfile") == "FROM python:3.12.9-slim\n"
    changed = git(world["a"], "diff", "--name-only", "main", br).split()
    assert sorted(changed) == ["Dockerfile", "api/Dockerfile"]                # nothing unrelated touched
    assert (world["a"] / "Dockerfile").read_text() == MULTI                   # main branch untouched
    # repo-b had nothing to change -> no branch
    assert git(world["b"], "branch", "--list", "docker-image-update/*").strip() == ""


def test_update_without_commit_is_flagged_not_persisted(world):
    rc, out = run(world, upgrade={"report_only": False}, git={"commit_changes": False})
    t = next((world["tmp"] / "dockerfile-reports").glob("repo-a-*.txt")).read_text()
    assert "NOT persisted" in t
    assert git(world["a"], "branch", "--list", "docker-image-update/*").strip() == ""


def test_selected_repositories_filter(world):
    rc, out = run(world, extra_env={"DIU_REPOSITORIES": "repo-b"})
    assert [r["Repo_Name"] for r in read_csv(out)] == ["repo-b"]


def test_env_dry_run_overrides_update_settings(world):
    rc, out = run(world, extra_env={"DIU_DRY_RUN": "true"}, upgrade={"report_only": False}, git={"commit_changes": True})
    assert git(world["a"], "branch", "--list", "docker-image-update/*").strip() == ""


def test_exit_code_policy(world):
    assert run(world)[0] == 0
    assert run(world, **{"exit": {"nonzero_on_repo_failure": True}})[0] == 1


def test_missing_inventory_is_fatal_exit_2(world, tmp_path):
    rc = main.run(["--config", str(settings_file(tmp_path)), "--repos-file", str(tmp_path / "nope.xlsx"), "--output-dir", str(tmp_path / "o")], env=ENV)
    assert rc == 2


def test_unexpected_exception_in_one_repo_is_isolated(world, monkeypatch):
    orig = main.ImageUpdater.process_repository
    def flaky(self, repo_dir, result):
        if result.repo_name == "repo-a":
            raise RuntimeError("kaboom")
        return orig(self, repo_dir, result)
    monkeypatch.setattr(main.ImageUpdater, "process_repository", flaky)
    rc, out = run(world)
    rows = {r["Repo_Name"]: r for r in read_csv(out)}
    assert rc == 0 and "kaboom" in rows["repo-a"]["Error"] and rows["repo-b"]["Total_Dockerfiles"] == "1"


def test_no_secrets_in_artifacts(world):
    rc, out = run(world, extra_env={"GITHUB_TOKEN": "ghp_SECRETVALUE_987654"})
    blob = "".join(p.read_text(errors="ignore") for p in out.rglob("*") if p.is_file() and p.suffix in {".txt", ".csv", ".log"})
    assert "ghp_SECRETVALUE_987654" not in blob and "pw-secret-123" not in blob


def test_excel_plus_secrets_mode_adds_secret_recipients_deduped(world):
    rc, out = run(world, extra_env={"EMAIL_TO": "A@example.com, secret-team@test.io", "EMAIL_CC": "secret-cc@test.io, secret-team@test.io"},
                  email={"recipients_source": "excel_plus_secrets"})
    msg, frm, to = FakeSMTP.instances[0].sent[0]
    assert len(to) == len(set(to)) and "secret-team@test.io" in msg["To"] and "secret-team@test.io" not in (msg["Cc"] or "")
    assert "secret-cc@test.io" in msg["Cc"] and to.count("a@example.com") == 1


def test_smtp_failure_does_not_fail_run_and_reports_remain(world, monkeypatch):
    import smtplib
    class Boom(FakeSMTP):
        def __init__(self, *a, **k):
            raise smtplib.SMTPConnectError(421, b"down")
    monkeypatch.setattr(main.email_sender.smtplib, "SMTP", Boom)
    rc, out = run(world)
    assert rc == 0 and list(out.glob("Dockerfile-update-report-*.csv")) and list((world["tmp"] / "dockerfile-reports").glob("repo-a-*.txt"))
