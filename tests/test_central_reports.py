"""Centralised per-repository reports: dockerfile-reports/{repo-name}-{DD-Mon-YYYY}.txt"""
from datetime import datetime, timezone

from src.models import RepoResult
from src.report_generator import format_report_date, write_repo_report


def test_date_format_is_locale_independent():
    assert format_report_date(datetime(2026, 11, 22, tzinfo=timezone.utc)) == "22-Nov-2026"
    assert format_report_date(datetime(2027, 1, 5)) == "05-Jan-2027"


def test_central_dir_created_and_names_follow_convention(tmp_path):
    out = tmp_path / "ws" / "dockerfile-reports"
    names = [write_repo_report(RepoResult(n, "l"), out, "2026-11-22", "22-Nov-2026").name for n in ("repo-one", "repo-two", "repo-three")]
    assert names == ["repo-one-22-Nov-2026.txt", "repo-two-22-Nov-2026.txt", "repo-three-22-Nov-2026.txt"]
    assert sorted(p.name for p in out.iterdir()) == sorted(names)


def test_duplicate_repo_names_do_not_overwrite(tmp_path):
    taken = set()
    a = write_repo_report(RepoResult("same", "l1"), tmp_path, "d", "22-Nov-2026", taken)
    b = write_repo_report(RepoResult("same", "l2"), tmp_path, "d", "22-Nov-2026", taken)
    assert a.name == "same-22-Nov-2026.txt" and b.name == "same-22-Nov-2026-2.txt" and len(list(tmp_path.iterdir())) == 2


def test_unsafe_repo_name_cannot_escape_the_folder(tmp_path):
    p = write_repo_report(RepoResult("../../etc/passwd", "l"), tmp_path / "c", "d", "22-Nov-2026")
    assert p.parent == tmp_path / "c"
