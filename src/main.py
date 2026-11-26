"""Entry point: python -m src.main [--config config/settings.yaml]"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import email_sender
from .config import load_settings
from .github_manager import GitAccessError, GitHubManager, RepoInput, TokenPool, clean_directory_contents, load_repositories, make_temp_dir
from .image_updater import ImageUpdater
from .logger import attach_repo_log, detach_repo_log, get_logger, setup_logging
from .models import RepoResult
from .registry_manager import RegistryClient
from .report_generator import (compute_overall, fmt_duration, format_overall, utc_now, write_consolidated_csv,
                               write_repo_report, zip_reports, format_report_date, safe_filename)
from .validator import Validator

log = get_logger("main")


def _clone_with_tokens(inp: RepoInput, settings: dict, gh, clone_dir: Path, res: RepoResult) -> GitHubManager:
    """Pick the token (Excel `Token_Name`, else try every configured token), select the branch, clone.

    Returns the manager (= token) that worked, so push and pull request use the very same token."""
    cands = gh.candidates(inp.token_name) if isinstance(gh, TokenPool) else [("GITHUB_TOKEN", gh)]
    priority = settings["git"].get("branch_priority") or []
    last: Optional[GitAccessError] = None
    for i, (name, mgr) in enumerate(cands):
        try:
            branch = mgr.select_branch(inp.repo_link, priority)
            mgr.clone(inp.repo_link, clone_dir, branch)
        except GitAccessError as exc:
            last = exc
            if exc.kind in {"auth", "not_found", "permission"} and i < len(cands) - 1:
                log.warning("%s: token %s has no access (%s) - trying the next token", inp.repo_name, name, exc.kind)
                continue
            raise
        res.token_name = name
        res.base_branch = branch or mgr.default_branch(clone_dir)
        log.info("%s: token=%s branch=%s%s", inp.repo_name, name, res.base_branch,
                 "" if branch else " (none of " + "/".join(priority) + " exists - repository default branch used)")
        if not branch and priority:
            res.notes.append(f"None of the branches {', '.join(priority)} exist; used the default branch '{res.base_branch}'")
        return mgr
    assert last is not None
    raise last


def process_repository(inp: RepoInput, settings: dict, gh, updater: ImageUpdater,
                       reports_dir: Path, date: str, clone_dir: Optional[Path] = None) -> RepoResult:
    """Fully isolated processing of a single repository. Never raises.

    The repository is cloned into `clone_dir` (repos/{repo-name}); it stays there until the whole run is finished."""
    res = RepoResult(inp.repo_name, inp.repo_link, inp.poc, inp.email, inp.cc_email)
    handler = attach_repo_log(inp.repo_name, Path(settings["output"]["logs_dir"]))
    work: Optional[Path] = None
    try:
        if inp.row_error:
            res.repo_error = f"Invalid inventory row: {inp.row_error}"
            return res
        if clone_dir is None:                      # fallback (direct callers): private temp dir, removed below
            work = make_temp_dir()
            clone_dir = work / "repo"
        else:
            clone_dir.parent.mkdir(parents=True, exist_ok=True)
        log.info("=== %s (%s)", inp.repo_name, inp.repo_link)
        gh = _clone_with_tokens(inp, settings, gh, clone_dir, res)
        updater.process_repository(clone_dir, res)

        g = settings["git"]
        changed = [d.path for d in res.dockerfiles if d.modified]
        if changed and g["commit_changes"]:
            _commit_and_pr(inp, res, clone_dir, changed, settings, gh, reports_dir, date)
        elif changed and not updater.report_only:
            res.notes.append("Dockerfiles were modified in a temporary clone only (commit_changes is disabled); "
                             "changes are NOT persisted to the repository.")
    except GitAccessError as exc:
        res.repo_error = f"{exc.kind}: {exc}"
        log.error("%s: %s", inp.repo_name, res.repo_error)
    except Exception as exc:  # noqa: BLE001 - repository failure must not stop the run
        res.repo_error = f"Unexpected error: {type(exc).__name__}: {exc}"
        log.exception("%s: unexpected failure", inp.repo_name)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
        detach_repo_log(handler)
    return res


def _commit_and_pr(inp, res, clone_dir, changed, settings, gh, reports_dir, date) -> None:
    g = settings["git"]
    branch = f"{g['branch_prefix']}/{utc_now().strftime('%Y%m%d-%H%M%S')}"
    files = list(changed)          # only Dockerfiles are committed - reports are never written into the repository
    base = res.base_branch or g.get("base_branch") or gh.default_branch(clone_dir)
    msg = f"chore(docker): update base image versions ({len(changed)} Dockerfile(s))"
    try:
        gh.commit_and_push(clone_dir, inp.repo_link, branch, files, msg, g["commit_author_name"], g["commit_author_email"])
        res.notes.append(f"Committed and pushed branch {branch}")
        if g["create_pull_requests"] and settings.get("testing", {}).get("enabled"):
            # TEST MODE: the pull request is NOT created
            res.notes.append(f"TEST MODE: pull request NOT created (would target '{base}')")
            log.warning("%s: TEST MODE - pull request skipped", inp.repo_name)
        elif g["create_pull_requests"]:
            body = ("Automated Docker base image update.\n\n"
                    + "\n".join(f"- `{r.dockerfile}`: `{r.old_image}` -> `{r.new_image}` ({r.upgrade_type}, {r.status})"
                                for d in res.dockerfiles for r in d.rows if r.status in {"UPDATED", "VALIDATION_NOT_EXECUTED"})
                    + "\n\nReview the validation notes before merging; major upgrades can introduce breaking changes.")
            res.pr_url = gh.create_pull_request(inp.repo_link, branch, base, msg, body)   # <-- PULL REQUEST CREATION (skipped in test mode)
            res.notes.append(f"Pull request: {res.pr_url}")
    except GitAccessError as exc:
        res.notes.append(f"Commit/push/PR failed ({exc.kind}): {exc}")
        log.error("%s: commit/PR failed: %s", inp.repo_name, exc)


def run(argv: Optional[list[str]] = None, env: Optional[dict] = None) -> int:
    ap = argparse.ArgumentParser(description="Docker image upgrade automation")
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--repos-file", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    if sys.version_info < (3, 11):
        print(f"Python 3.11+ is required (3.15 recommended); found {sys.version.split()[0]}", file=sys.stderr)
        return 2

    settings = load_settings(args.config, env)
    if args.repos_file:
        settings["input"]["repositories_file"] = args.repos_file
    if args.output_dir:
        settings["output"]["reports_dir"] = args.output_dir
        settings["output"]["logs_dir"] = str(Path(args.output_dir) / "logs")
    if args.no_email:
        settings["email"]["enabled"] = False

    from .github_manager import discover_token_names
    token_names = discover_token_names(env)
    all_tokens = [env[n].strip() for n in token_names]
    setup_logging(settings["output"]["log_level"], [*all_tokens, env.get("SMTP_PASSWORD"), env.get("DOCKERHUB_TOKEN")])
    started = utc_now()
    t0 = time.monotonic()
    date = started.strftime("%Y-%m-%d")
    reports_dir = Path(settings["output"]["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)

    up = settings["upgrade"]
    mode = ("REPORT-ONLY (no Dockerfiles modified)" if up["report_only"]
            else ("UPDATE + PULL REQUEST" if settings["git"]["create_pull_requests"]
                  else "UPDATE (commit)" if settings["git"]["commit_changes"] else "UPDATE (temporary clone only)"))
    log.info("Starting Docker image update | strategy=%s | mode=%s | auto_major=%s", up["strategy"], mode, up["auto_apply_major"])
    if settings.get("testing", {}).get("enabled"):
        log.warning("TEST MODE is ON - the e-mail will NOT be sent and pull requests will NOT be created")
    if token_names:
        log.info("GitHub tokens configured: %s (names only)", ", ".join(token_names))
    else:
        log.warning("no GitHub token configured (GIT_TOKEN, GIT_TOKEN2, ...) - only public repositories can be processed")

    try:
        inputs = load_repositories(settings["input"]["repositories_file"])
    except (OSError, ValueError) as exc:
        log.error("cannot read repository inventory: %s", exc)
        return 2
    selected = {s.lower() for s in settings["input"].get("selected_repositories", [])}
    if selected:
        inputs = [i for i in inputs if i.repo_name.lower() in selected]
        log.info("Selected repositories filter active: %d match(es)", len(inputs))
    if not inputs:
        log.error("no repositories to process")
        return 2

    rcfg = settings["registry"]
    registry = RegistryClient(timeout=rcfg["timeout_seconds"], retries=rcfg["retry_count"],
                              backoff=rcfg["backoff_seconds"], max_pages=rcfg["max_tag_pages"], env=env)
    gh = TokenPool(env, retries=rcfg["retry_count"])
    updater = ImageUpdater(settings, registry, Validator(settings))

    # Every repository is cloned into <clones_dir>/<repo-name>/ (default repos/<repo-name>/) and stays there for the whole run.
    clones_root = Path(settings["output"]["clones_dir"])
    used_names: set = set()
    clone_dirs: list[Path] = []
    results: list[RepoResult] = []
    for inp in inputs:
        base = safe_filename(inp.repo_name)
        name, i = base, 2
        while name.lower() in used_names:
            name, i = f"{base}-{i}", i + 1
        used_names.add(name.lower())
        cd = clones_root / name
        clone_dirs.append(cd)
        results.append(process_repository(inp, settings, gh, updater, reports_dir, date, cd))

    # ---- reports (failures here are logged, never fatal) ----
    # Every repository report goes to ONE central folder (created when missing), named {repo-name}-{DD-Mon-YYYY}.txt.
    central = Path(settings["output"]["dockerfile_reports_dir"])
    file_date = format_report_date(started)
    taken: set = set()
    repo_reports: list[Path] = []
    for r in results:
        try:
            repo_reports.append(write_repo_report(r, central, date, file_date, taken))
        except OSError as exc:
            log.error("could not write report for %s: %s", r.repo_name, exc)
    # availability check - a missing report is logged, never fatal
    available = 0
    for r in results:
        p = Path(r.report_path) if r.report_path else None
        if p and p.is_file() and p.stat().st_size > 0:
            available += 1
        else:
            log.warning("%s: no report available in %s", r.repo_name, central)
    centralized = f"Centralized reports: {available} of {len(results)} available in {central}/"
    log.info(centralized)
    csv_path = None
    try:
        csv_path = write_consolidated_csv(results, reports_dir, date)
    except OSError as exc:
        log.error("could not write consolidated CSV: %s", exc)
    zip_path = None
    try:
        zip_path = zip_reports(repo_reports, reports_dir / f"Docker-repository-reports-{date}.zip")
    except OSError as exc:
        log.error("could not create ZIP: %s", exc)

    finished = utc_now()
    summary = compute_overall(results, date)
    summary.mode = mode
    summary.scheduled_time = env.get("SCHEDULED_TIME", "")
    summary.started, summary.finished = started.strftime("%Y-%m-%d %H:%M:%S UTC"), finished.strftime("%Y-%m-%d %H:%M:%S UTC")
    summary.duration = fmt_duration(time.monotonic() - t0)

    # ---- email ----
    if settings["email"]["enabled"]:
        ecfg = email_sender.config_from_env(env)
        rows = [(i.repo_name, i.email, i.cc_email) for i in inputs]          # every processed Excel row
        if settings["email"]["recipients_source"] == "excel_plus_secrets":
            rows.append(("(secrets)", ",".join(ecfg.to), ",".join(ecfg.cc)))
        plan = email_sender.collect_recipients(rows)
        plan.repositories = len(inputs)
        ecfg.to, ecfg.cc = plan.to, plan.cc
        for repo, tok in plan.invalid:
            log.warning("email recipients: skipped invalid address %s (repository %s)", tok, repo)
        rec_summary = email_sender.format_recipient_summary(plan)
        log.info("\n%s", rec_summary)
        (reports_dir / "email-recipient-summary.txt").write_text(rec_summary, encoding="utf-8")
        atts = [p for p in (csv_path, zip_path) if p]
        subject = settings["email"]["subject"].format(date=date)
        if settings.get("testing", {}).get("enabled"):
            # TEST MODE: build the message (so it is validated) and save a preview, but do NOT send it
            summary.email_status = "TEST MODE - email NOT sent"
            try:
                msg = email_sender.build_message(ecfg, subject, summary, atts)
                (reports_dir / "email-preview-NOT-SENT.eml").write_bytes(bytes(msg))
                summary.email_status += " (preview saved: email-preview-NOT-SENT.eml)"
            except (ValueError, OSError) as exc:
                summary.email_status += f" (preview not built: {exc})"
            log.warning("email: %s", summary.email_status)
        else:
            er = email_sender.send_report(ecfg, subject, summary, atts,    # <-- EMAIL SENDING (skipped in test mode)
                                          settings["email"]["use_tls"], settings["email"]["timeout_seconds"])
            summary.email_status = er.message
            (log.info if er.sent else log.error)("email: %s", er.message)
    else:
        summary.email_status = "disabled"

    # ---- clean-up: only AFTER every repository is done and the summary e-mail step has finished ----
    if settings["output"].get("keep_clones"):
        log.info("output.keep_clones is true - clones kept in %s/", clones_root)
    else:
        removed = sum(clean_directory_contents(d) for d in clone_dirs)
        log.info("Clean-up: removed the cloned code from %s/<repo-name>/ (%d entries)", clones_root, removed)

    text = format_overall(summary)
    (reports_dir / "overall-summary.txt").write_text(text + f"\nEmail: {summary.email_status}\n{centralized}\n", encoding="utf-8")
    print("\n" + text + f"Email: {summary.email_status}\n{centralized}")
    step = env.get("GITHUB_STEP_SUMMARY")
    if step:
        try:
            with open(step, "a", encoding="utf-8") as fh:
                fh.write("```\n" + text + "```\n")
        except OSError:
            pass
    if settings["exit"]["nonzero_on_repo_failure"] and summary.failed_repos:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
