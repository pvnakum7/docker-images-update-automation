"""Per-repository text reports, consolidated CSV, overall summary and ZIP packaging."""
from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import RepoResult
from .tech_detector import summarize

CSV_COLUMNS = ["Repo_Name", "Repo_Link", "POC", "Email", "CC_Email", "Total_Dockerfiles", "Total_FROM_Instructions",
               "Updated_Dockerfiles", "Updated_Images", "Skipped_Dockerfiles", "Skipped_Images", "Failed_Dockerfiles",
               "Failed_Images", "Up_To_Date_Images", "Major_Upgrade_Recommendations", "Overall_Status",
               # extension columns (appended so the required columns keep their documented order)
               "Up_To_Date_Dockerfiles", "Pending_Dockerfiles", "Pending_Images", "Technologies", "Pull_Request", "Error"]

TABLE_HEADERS = ["Dockerfile Location", "Registry", "Image Name", "Old Image Version", "New Image Version",
                 "Upgrade Type", "Status", "Remarks"]


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "repo"


def csv_safe(value) -> str:
    """Neutralise spreadsheet formula injection for values that came from user-edited input."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in {"=", "+", "-", "@", "\t", "\r"} else s


def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    def line(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    sep = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), sep, *(line(r) for r in rows)])


def tech_summary_lines(result: RepoResult) -> list[str]:
    by = summarize(result.technologies)
    if not by:
        return ["  No supported technology detected"]
    out = []
    for name, finds in by.items():
        locs = sorted({f.directory for f in finds})
        hints = sorted({h for f in finds for h in f.hints})
        out.append(f"  - {name}: {', '.join(locs)}" + (f"  [{'; '.join(hints)}]" if hints else ""))
    return out


def technologies_inline(result: RepoResult) -> str:
    return ", ".join(summarize(result.technologies).keys())


def build_repo_report(result: RepoResult, date: str) -> str:
    L: list[str] = []
    L.append(f"Repository: {result.repo_name}")
    L.append(f"Date: {date}")
    L.append(f"Repository URL: {result.repo_link}")
    if result.base_branch:
        L.append(f"Branch Analysed: {result.base_branch}")
    if result.token_name:
        L.append(f"GitHub Token Used: {result.token_name}")
    if result.pr_url:
        L.append(f"Pull Request: {result.pr_url}")
    L.append("")
    if result.repo_error:
        L.append(f"REPOSITORY PROCESSING FAILED: {result.repo_error}")
        L.append("")
    L.append("Technology Detection Summary")
    L += tech_summary_lines(result)
    L.append("")
    L.append('"Dockerfile Image Update Details"')
    rows = []
    for d in sorted(result.dockerfiles, key=lambda x: x.path):
        for r in d.rows:
            rows.append([r.dockerfile, r.registry, r.image_name, r.old_image, r.new_image, r.upgrade_type,
                         r.status, r.remarks])
        if d.error:
            rows.append([d.path, "-", "-", "-", "-", "-", "FAILED", d.error])
        if not d.rows and not d.error:
            rows.append([d.path, "-", "-", "-", "-", "-", "SKIPPED", "No FROM instructions found"])
    L.append(_fmt_table(TABLE_HEADERS, rows) if rows else "(no Dockerfiles found)")
    L.append("")
    L.append("Validation Details")
    vrows = [[d.path, d.validation_status if d.validation_status != "NOT_RUN" else "NOT_RUN (nothing modified)",
              d.validation_details or "-"] for d in sorted(result.dockerfiles, key=lambda x: x.path)]
    L.append(_fmt_table(["Dockerfile Location", "Validation", "Details"], vrows) if vrows else "(none)")
    if result.notes:
        L.append("")
        L.append("Notes")
        L += [f"  - {n}" for n in result.notes]
    L.append("")
    L.append("Repo Dockerfile Image Update Summary:")
    L.append("")
    L.append(f"Date: {date}")
    L.append(f"Repository Name: {result.repo_name}")
    L.append(f"Repository Link: {result.repo_link}")
    L.append("")
    L.append(f"Total Dockerfiles: {result.total_dockerfiles}")
    L.append(f"Total FROM instructions: {result.total_from}")
    L.append(f"Updated Images: {result.updated_images}")
    L.append(f"Skipped Images: {result.skipped_images}")
    L.append(f"Failed Images: {result.failed_images}")
    L.append(f"Up-to-date Images: {result.up_to_date_images}")
    L.append(f"Pending (update proposed, not applied) Images: {result.pending_images}")
    L.append(f"Major Upgrade Recommendations: {result.major_recommendations}")
    L.append(f"Overall Status: {result.overall_status}")
    return "\n".join(L) + "\n"


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_report_date(dt) -> str:
    """22-Nov-2026 (fixed English month names: independent of the machine's locale)."""
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def write_repo_report(result: RepoResult, out_dir: Path, date: str, file_date: Optional[str] = None,
                      taken: Optional[set] = None) -> Path:
    """Write `{repo-name}-{file_date}.txt` into out_dir (created when missing).

    `taken` (names already used in this run) keeps two inventory rows with the same name from overwriting each other."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_filename(result.repo_name)}-{file_date or date}"
    name, i = f"{stem}.txt", 2
    while taken is not None and name.lower() in taken:
        name, i = f"{stem}-{i}.txt", i + 1
    if taken is not None:
        taken.add(name.lower())
    path = out_dir / name
    path.write_text(build_repo_report(result, date), encoding="utf-8")
    result.report_path = str(path)
    return path


def write_consolidated_csv(results: list[RepoResult], out_dir: Path, date: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"Dockerfile-update-report-{date}.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in results:
            w.writerow([csv_safe(x) for x in [
                r.repo_name, r.repo_link, r.poc, r.email, r.cc_email, r.total_dockerfiles, r.total_from,
                r.updated_dockerfiles, r.updated_images, r.skipped_dockerfiles, r.skipped_images,
                r.failed_dockerfiles, r.failed_images, r.up_to_date_images, r.major_recommendations,
                r.overall_status, r.up_to_date_dockerfiles, r.pending_dockerfiles, r.pending_images,
                technologies_inline(r), r.pr_url or "", r.repo_error or ""]])
    return path


@dataclass
class OverallSummary:
    date: str
    total_repos: int = 0
    ok_repos: int = 0
    failed_repos: int = 0
    total_dockerfiles: int = 0
    updated_dockerfiles: int = 0
    skipped_dockerfiles: int = 0
    failed_dockerfiles: int = 0
    up_to_date_dockerfiles: int = 0
    pending_dockerfiles: int = 0
    total_from: int = 0
    updated_images: int = 0
    skipped_images: int = 0
    failed_images: int = 0
    up_to_date_images: int = 0
    pending_images: int = 0
    major_recommendations: int = 0
    status: str = "SUCCESS"
    mode: str = ""
    scheduled_time: str = ""
    started: str = ""
    finished: str = ""
    duration: str = ""
    email_status: str = ""


def compute_overall(results: list[RepoResult], date: str) -> OverallSummary:
    s = OverallSummary(date=date, total_repos=len(results))
    for r in results:
        if r.repo_error:
            s.failed_repos += 1
        else:
            s.ok_repos += 1
        s.total_dockerfiles += r.total_dockerfiles
        s.updated_dockerfiles += r.updated_dockerfiles
        s.skipped_dockerfiles += r.skipped_dockerfiles
        s.failed_dockerfiles += r.failed_dockerfiles
        s.up_to_date_dockerfiles += r.up_to_date_dockerfiles
        s.pending_dockerfiles += r.pending_dockerfiles
        s.total_from += r.total_from
        s.updated_images += r.updated_images
        s.skipped_images += r.skipped_images
        s.failed_images += r.failed_images
        s.up_to_date_images += r.up_to_date_images
        s.pending_images += r.pending_images
        s.major_recommendations += r.major_recommendations
    if s.total_repos and s.failed_repos == s.total_repos:
        s.status = "FAILED"
    elif s.failed_repos or s.failed_dockerfiles or s.failed_images:
        s.status = "PARTIAL_SUCCESS"
    return s


def format_overall(s: OverallSummary) -> str:
    lines = [
        "Overall Dockerfile Update Report", "",
        f"Date: {s.date}",
        f"Mode: {s.mode}" if s.mode else "",
        f"Scheduled Time (UTC): {s.scheduled_time or 'manual / n.a.'}",
        f"Execution Start: {s.started}",
        f"Execution End: {s.finished}",
        f"Duration: {s.duration}",
        f"Overall Status: {s.status}", "",
        f"Total Repositories: {s.total_repos}",
        f"Successfully Processed Repositories: {s.ok_repos}",
        f"Failed Repositories: {s.failed_repos}", "",
        f"Total Dockerfiles: {s.total_dockerfiles}",
        f"Updated Dockerfiles: {s.updated_dockerfiles}",
        f"Skipped Dockerfiles: {s.skipped_dockerfiles}",
        f"Failed Dockerfiles: {s.failed_dockerfiles}",
        f"Up-to-date Dockerfiles: {s.up_to_date_dockerfiles}",
        f"Dockerfiles With Pending Updates (not applied): {s.pending_dockerfiles}", "",
        f"Total FROM Instructions: {s.total_from}",
        f"Updated Images: {s.updated_images}",
        f"Skipped Images: {s.skipped_images}",
        f"Failed Images: {s.failed_images}",
        f"Up-to-date Images: {s.up_to_date_images}",
        f"Pending Images (not applied): {s.pending_images}",
        f"Major Upgrade Recommendations: {s.major_recommendations}",
    ]
    return "\n".join(l for i, l in enumerate(lines) if l != "" or (i and lines[i - 1] != "")) + "\n"


def zip_reports(report_paths: list[Path], zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in report_paths:
            if p and Path(p).exists():
                z.write(p, arcname=Path(p).name)
    return zip_path


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
