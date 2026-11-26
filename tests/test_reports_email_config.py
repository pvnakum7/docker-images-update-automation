import csv
import io
import logging
import smtplib
import zipfile
from pathlib import Path

import pytest
import yaml

from src import email_sender
from src.config import load_settings, normalise
from src.logger import _RedactingFilter, redact, register_secret
from src.models import DockerfileResult, ImageRow, RepoResult, TechFinding
from src.report_generator import (CSV_COLUMNS, build_repo_report, compute_overall, csv_safe, format_overall,
                                  write_consolidated_csv, write_repo_report, zip_reports)

ROOT = Path(__file__).resolve().parents[1]


def row(df, idx, status, kind="Same Major", old="node:22.22.0-slim", new="node:22.23.0-slim", reg="Docker Hub", name="node", rem="Stable update"):
    return ImageRow(df, idx, reg, name, old, new, kind, status, rem)


def make_repo(name="my-app"):
    r = RepoResult(name, f"https://github.com/org/{name}", "Person-A", "a@example.com", "cc@example.com")
    r.technologies = [TechFinding("Node.js", ["package.json"], ["node >=20 (.nvmrc)"], {}, "."), TechFinding("React", ["package.json"], [], {}, ".")]
    d1 = DockerfileResult("Dockerfile", ["Node.js"], 2, modified=True, validation_status="PASSED", validation_details="static ok")
    d1.rows = [row("Dockerfile", 1, "UPDATED"), row("Dockerfile", 2, "UPDATED", old="nginx:1.27-alpine", new="nginx:1.28-alpine", name="nginx"),
               row("Dockerfile", 1, "REQUIRES_VALIDATION", "New Major", new="node:26.13.0-slim", rem="Breaking changes possible")]
    d1.outcomes = {1: "UPDATED", 2: "UPDATED"}
    d1.major_flags = {1}
    d2 = DockerfileResult("services/api/Dockerfile", [], 1)
    d2.rows = [row("services/api/Dockerfile", 1, "SKIPPED", old="ghcr.io/org/api:v2.0", new="-", reg="GHCR", name="ghcr.io/org/api", rem="Unable to determine stable version")]
    d2.outcomes = {1: "SKIPPED"}
    d3 = DockerfileResult("web/Dockerfile", [], 2)
    d3.rows = [row("web/Dockerfile", 1, "UP_TO_DATE"), row("web/Dockerfile", 2, "FAILED", rem="boom")]
    d3.outcomes = {1: "UP_TO_DATE", 2: "FAILED"}
    r.dockerfiles = [d1, d2, d3]
    return r


def test_counts_distinguish_dockerfiles_from_from_instructions():
    r = make_repo()
    assert (r.total_dockerfiles, r.total_from) == (3, 5)
    assert (r.updated_dockerfiles, r.updated_images) == (1, 2)
    assert (r.skipped_dockerfiles, r.skipped_images) == (1, 1)
    assert (r.failed_dockerfiles, r.failed_images) == (1, 1)
    assert r.up_to_date_images == 1 and r.major_recommendations == 1
    assert r.overall_status == "COMPLETED_WITH_ERRORS"


def test_repo_report_content():
    txt = build_repo_report(make_repo(), "2026-11-20")
    for needle in ["Repository: my-app", "Date: 2026-11-20", "Repository URL: https://github.com/org/my-app",
                   "Technology Detection Summary", "- Node.js: .", "- React: .", '"Dockerfile Image Update Details"',
                   "Dockerfile Location", "Old Image Version", "New Image Version", "Upgrade Type",
                   "node:22.22.0-slim", "node:22.23.0-slim", "REQUIRES_VALIDATION", "New Major", "GHCR",
                   "Repo Dockerfile Image Update Summary:", "Repository Name: my-app",
                   "Total Dockerfiles: 3", "Total FROM instructions: 5", "Updated Images: 2", "Skipped Images: 1",
                   "Failed Images: 1", "Up-to-date Images: 1", "Major Upgrade Recommendations: 1"]:
        assert needle in txt, needle
    assert txt.rstrip().splitlines()[-1].startswith("Overall Status")


def test_repo_report_for_failed_repo(tmp_path):
    r = RepoResult("gone", "https://github.com/o/gone", repo_error="not_found: repository not found")
    p = write_repo_report(r, tmp_path / "new-dir", "2026-11-20", "20-Nov-2026")      # directory is created when missing
    assert p.name == "gone-20-Nov-2026.txt" and p.parent.name == "new-dir"
    t = p.read_text()
    assert "REPOSITORY PROCESSING FAILED: not_found" in t and "Total Dockerfiles: 0" in t and "Overall Status: FAILED" in t


def test_csv_one_row_per_repo_with_required_columns(tmp_path):
    a, b = make_repo("a"), RepoResult("b", "https://github.com/o/b", "P", "p@x", "", repo_error="auth: bad token")
    path = write_consolidated_csv([a, b], tmp_path, "2026-11-20")
    assert path.name == "Dockerfile-update-report-2026-11-20.csv"
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    assert len(rows) == 2
    assert CSV_COLUMNS[:16] == ["Repo_Name", "Repo_Link", "POC", "Email", "CC_Email", "Total_Dockerfiles", "Total_FROM_Instructions",
        "Updated_Dockerfiles", "Updated_Images", "Skipped_Dockerfiles", "Skipped_Images", "Failed_Dockerfiles", "Failed_Images",
        "Up_To_Date_Images", "Major_Upgrade_Recommendations", "Overall_Status"]
    assert list(rows[0].keys()) == CSV_COLUMNS
    ra = rows[0]
    assert (ra["Total_Dockerfiles"], ra["Total_FROM_Instructions"], ra["Updated_Dockerfiles"], ra["Updated_Images"]) == ("3", "5", "1", "2")
    assert (ra["Skipped_Dockerfiles"], ra["Failed_Dockerfiles"], ra["Failed_Images"], ra["Up_To_Date_Images"]) == ("1", "1", "1", "1")
    assert ra["Overall_Status"] == "COMPLETED_WITH_ERRORS" and ra["Technologies"] == "Node.js, React"
    assert rows[1]["Overall_Status"] == "FAILED" and "bad token" in rows[1]["Error"] and rows[1]["Total_Dockerfiles"] == "0"


def test_csv_formula_injection_neutralised(tmp_path):
    r = RepoResult("x", "https://g/x", poc="=HYPERLINK(\"http://evil\")", email="+cmd", cc_email="@x")
    path = write_consolidated_csv([r], tmp_path, "d")
    row = next(csv.DictReader(io.StringIO(path.read_text())))
    assert row["POC"].startswith("'=") and row["Email"].startswith("'+") and row["CC_Email"].startswith("'@")
    assert csv_safe("normal") == "normal"


def test_overall_summary_from_actual_results(capsys):
    a, b = make_repo("a"), make_repo("b")
    c = RepoResult("c", "l", repo_error="x")
    s = compute_overall([a, b, c], "2026-11-20")
    assert (s.total_repos, s.ok_repos, s.failed_repos) == (3, 2, 1)
    assert (s.total_dockerfiles, s.updated_dockerfiles, s.skipped_dockerfiles, s.failed_dockerfiles) == (6, 2, 2, 2)
    assert (s.total_from, s.updated_images, s.skipped_images, s.failed_images, s.up_to_date_images) == (10, 4, 2, 2, 2)
    assert s.status == "PARTIAL_SUCCESS"
    txt = format_overall(s)
    assert "Total Repositories: 3" in txt and "Successfully Processed Repositories: 2" in txt and "Failed Repositories: 1" in txt
    assert compute_overall([c], "d").status == "FAILED"
    assert compute_overall([RepoResult("ok", "l")], "d").status == "SUCCESS"


def test_zip(tmp_path):
    p1 = write_repo_report(make_repo("a"), tmp_path / "r", "d", "22-Nov-2026")
    p2 = write_repo_report(make_repo("b"), tmp_path / "r", "d", "22-Nov-2026")
    z = zip_reports([p1, p2], tmp_path / "x.zip")
    assert sorted(zipfile.ZipFile(z).namelist()) == ["a-22-Nov-2026.txt", "b-22-Nov-2026.txt"]


# ------------------------------------------------------------------ email
class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None, fail_login=False):
        self.host, self.port = host, port
        self.sent, self.tls, self.login_args, self.fail_login = [], False, None, fail_login
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, u, p):
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.login_args = (u, p)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append((msg, from_addr, to_addrs))


def cfg(**kw):
    base = dict(server="smtp.example.com", port=587, username="u", password="s3cret-pw", sender="bot@example.com",
                to=["a@example.com", "b@example.com"], cc=["c@example.com"])
    base.update(kw)
    return email_sender.EmailConfig(**base)


def test_email_message_recipients_and_attachments(tmp_path):
    csvp = tmp_path / "r.csv"; csvp.write_text("a,b\n")
    zp = zip_reports([write_repo_report(make_repo(), tmp_path / "r", "d")], tmp_path / "x.zip")
    s = compute_overall([make_repo()], "2026-11-20")
    msg = email_sender.build_message(cfg(), "Docker Image Upgrade Automation Report - 2026-11-20", s, [csvp, zp])
    assert msg["To"] == "a@example.com, b@example.com" and msg["Cc"] == "c@example.com"
    assert msg["Subject"] == "Docker Image Upgrade Automation Report - 2026-11-20"
    assert sorted(a.get_filename() for a in msg.iter_attachments()) == ["r.csv", "x.zip"]
    body = msg.get_body().get_content()
    assert "Total Repositories: 1" in body and "Updated Dockerfiles: 1" in body and "Hello Team" in body and "DevOps Automation" in body


def test_email_send_starttls_login_and_cc_delivery(tmp_path):
    FakeSMTP.instances.clear()
    s = compute_overall([make_repo()], "d")
    res = email_sender.send_report(cfg(), "subj", s, [], smtp_factory=FakeSMTP)
    srv = FakeSMTP.instances[0]
    assert res.sent and srv.tls and srv.login_args == ("u", "s3cret-pw")
    assert set(srv.sent[0][2]) == {"a@example.com", "b@example.com", "c@example.com"}


def test_email_ssl_port_skips_starttls(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", FakeSMTP)
    res = email_sender.send_report(cfg(port=465), "subj", compute_overall([], "d"), [])
    assert res.sent and not FakeSMTP.instances[0].tls


def test_email_failures_do_not_raise():
    s = compute_overall([], "d")
    assert not email_sender.send_report(cfg(server=""), "x", s, []).sent
    assert "authentication" in email_sender.send_report(cfg(), "x", s, [], smtp_factory=lambda h, p, timeout=None: FakeSMTP(h, p, fail_login=True)).message
    def boom(h, p, timeout=None):
        raise OSError("connection refused")
    assert "SMTP error" in email_sender.send_report(cfg(), "x", s, [], smtp_factory=boom).message
    assert "invalid email" in email_sender.send_report(cfg(to=["not-an-email"]), "x", s, []).message
    assert "newline" in email_sender.send_report(cfg(), "bad\nsubject", s, []).message


def test_email_env_parsing_multiple_recipients():
    c = email_sender.config_from_env({"SMTP_SERVER": "s", "SMTP_PORT": "2525", "EMAIL_FROM": "f@x.io",
                                      "EMAIL_TO": "a@x.io, b@x.io;c@x.io", "EMAIL_CC": "Dev Team <d@x.io>,e@x.io"})
    assert c.to == ["a@x.io", "b@x.io", "c@x.io"] and c.cc == ["d@x.io", "e@x.io"] and c.port == 2525 and c.configured


# ------------------------------------------------------------------ config / logging
def test_safe_defaults():
    s = load_settings(ROOT / "config/settings.yaml", {})
    assert s["upgrade"]["report_only"] is True and s["upgrade"]["auto_apply_major"] is False
    assert s["git"]["commit_changes"] is False and s["git"]["create_pull_requests"] is False
    assert s["upgrade"]["max_dockerfile_depth"] == 4 and s["upgrade"]["digest_policy"] == "skip"
    assert s["validation"]["technology"]["enabled"] is False and s["validation"]["docker_build"] is False


def test_env_overrides_and_invariants():
    s = load_settings(None, {"DIU_REPORT_ONLY": "false", "DIU_CREATE_PR": "true", "DIU_STRATEGY": "BOTH", "DIU_REPOSITORIES": "a, b"})
    assert s["upgrade"]["report_only"] is False and s["git"]["create_pull_requests"] and s["git"]["commit_changes"]
    assert s["input"]["selected_repositories"] == ["a", "b"]
    s = load_settings(None, {"DIU_REPORT_ONLY": "false", "DIU_CREATE_PR": "true", "DIU_DRY_RUN": "true"})
    assert s["upgrade"]["report_only"] and not s["git"]["create_pull_requests"] and not s["git"]["commit_changes"]
    with pytest.raises(ValueError):
        load_settings(None, {"DIU_STRATEGY": "yolo"})


def test_secret_redaction():
    register_secret("ghp_supersecrettoken123")
    assert "ghp_supersecrettoken123" not in redact("clone https://x:ghp_supersecrettoken123@github.com failed")
    rec = logging.LogRecord("diu.x", logging.INFO, "f", 1, "token=%s", ("ghp_supersecrettoken123",), None)
    _RedactingFilter().filter(rec)
    assert "ghp_supersecrettoken123" not in rec.getMessage()


def test_settings_yaml_is_valid_and_complete():
    raw = yaml.safe_load((ROOT / "config/settings.yaml").read_text())
    for k in ["upgrade", "registry", "validation", "git", "email", "output"]:
        assert k in raw
    for k in ["strategy", "report_only", "auto_apply_major", "max_dockerfile_depth"]:
        assert k in raw["upgrade"]
    for k in ["timeout_seconds", "retry_count"]:
        assert k in raw["registry"]
