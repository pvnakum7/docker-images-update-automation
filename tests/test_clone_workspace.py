"""Clones live in repos/{repo-name}/ for the whole run and are deleted only after the summary e-mail step."""
import os
import stat

import pytest

import src.main as main
import src.email_sender as es
from src.github_manager import clean_directory_contents
from tests.helpers import write
from tests.test_e2e_main import ENV, make_repo, settings_file
from tests.test_reports_email_config import FakeSMTP
from tests.test_tokens_branches_testmode import make_world, run


def two_repos(tmp_path, monkeypatch, extra_rows=()):
    a = make_repo(tmp_path / "s/a", {"Dockerfile": "FROM python:3.12.5-slim\n", "sub/Dockerfile.dev": "FROM nginx:1.27-alpine\n"})
    b = make_repo(tmp_path / "s/b", {"Dockerfile": "FROM scratch\n"})
    rows = [["repo-a", str(a), "p", "a@x.io", "", ""], ["repo-b", str(b), "p", "b@x.io", "", ""], *extra_rows]
    return make_world(tmp_path, monkeypatch, rows)


def test_clones_in_repos_dir_exist_until_email_step_then_are_cleaned(tmp_path, monkeypatch):
    inv = two_repos(tmp_path, monkeypatch)
    seen = {}

    class Spy(FakeSMTP):
        def send_message(self, msg, from_addr=None, to_addrs=None):          # the summary e-mail is being sent right now
            for r in ("repo-a", "repo-b"):
                seen[r] = sorted(p.name for p in (tmp_path / "repos" / r).iterdir())
            super().send_message(msg, from_addr, to_addrs)
    monkeypatch.setattr(es.smtplib, "SMTP", Spy)
    rc, out = run(tmp_path, inv, env={"DIU_TEST_MODE": "false"})
    assert "Dockerfile" in seen["repo-a"] and ".git" in seen["repo-a"] and "sub" in seen["repo-a"] and "Dockerfile" in seen["repo-b"]
    for r in ("repo-a", "repo-b"):                                              # after the e-mail: contents gone
        assert list((tmp_path / "repos" / r).iterdir()) == []


def test_cleanup_also_happens_in_test_mode_and_when_email_fails(tmp_path, monkeypatch):
    inv = two_repos(tmp_path, monkeypatch)
    rc, out = run(tmp_path, inv, env={"DIU_TEST_MODE": "true"})
    assert all(list(d.iterdir()) == [] for d in (tmp_path / "repos").iterdir())
    def boom(*a, **k):
        raise OSError("smtp down")
    monkeypatch.setattr(es.smtplib, "SMTP", boom)
    rc, out = run(tmp_path, inv, env={"DIU_TEST_MODE": "false"})
    assert rc == 0 and all(list(d.iterdir()) == [] for d in (tmp_path / "repos").iterdir())


def test_reports_survive_cleanup_and_are_not_inside_clones(tmp_path, monkeypatch):
    inv = two_repos(tmp_path, monkeypatch)
    rc, out = run(tmp_path, inv)
    assert len(list((tmp_path / "dockerfile-reports").glob("*.txt"))) == 2
    assert not list((tmp_path / "repos").rglob("*.txt"))


def test_keep_clones_skips_cleanup(tmp_path, monkeypatch):
    inv = two_repos(tmp_path, monkeypatch)
    rc, out = run(tmp_path, inv, output={"keep_clones": True})
    assert (tmp_path / "repos/repo-a/Dockerfile").exists() and (tmp_path / "repos/repo-b/Dockerfile").exists()


def test_leftover_clone_from_previous_run_is_replaced_and_duplicate_names_get_own_dirs(tmp_path, monkeypatch):
    stale = tmp_path / "repos/repo-a"
    write(stale / "OLD-FILE", "stale")
    b2 = make_repo(tmp_path / "s/b2", {"Dockerfile": "FROM scratch\n"})
    inv = two_repos(tmp_path, monkeypatch, extra_rows=[["repo-a", str(b2), "p", "c@x.io", "", ""]])
    rc, out = run(tmp_path, inv, output={"keep_clones": True})
    assert not (tmp_path / "repos/repo-a/OLD-FILE").exists()                      # fresh clone, nothing stale
    assert (tmp_path / "repos/repo-a-2/Dockerfile").exists() and (tmp_path / "repos/repo-a/sub").exists()


def test_failed_clone_leaves_no_files_and_run_continues(tmp_path, monkeypatch):
    inv = two_repos(tmp_path, monkeypatch, extra_rows=[["gone", "file://" + str(tmp_path / "nope"), "p", "g@x.io", "", ""]])
    rc, out = run(tmp_path, inv)
    assert rc == 0 and list((tmp_path / "repos/gone").glob("*")) == []


def test_clean_directory_contents_handles_readonly_files_and_symlinks(tmp_path):
    d = tmp_path / "r"
    write(d / "x/ro.txt", "1")
    os.chmod(d / "x/ro.txt", stat.S_IREAD)
    outside = tmp_path / "outside.txt"; outside.write_text("keep")
    (d / "link").symlink_to(outside)
    assert clean_directory_contents(d) == 2
    assert d.is_dir() and list(d.iterdir()) == [] and outside.read_text() == "keep"       # target of a symlink is never touched
    assert clean_directory_contents(tmp_path / "missing") == 0
