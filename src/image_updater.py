"""Per-repository orchestration: evaluate every FROM under both strategies, apply safely, validate, restore."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from . import version_manager as vm
from .dockerfile_parser import (InvalidImageReference, ParsedDockerfile, parse_dockerfile, parse_image_ref,
                                rewrite_from)
from .dockerfile_scanner import find_dockerfiles
from .logger import get_logger
from .models import (APPLIED_STATUSES, FAILED, FAILED_STATUSES, INVALID_IMAGE, MAJOR_UPGRADE_RECOMMENDED, NEW_MAJOR,
                     NOT_SUPPORTED, REQUIRES_VALIDATION, SAME_MAJOR, SKIPPED, UP_TO_DATE, UPDATE_AVAILABLE, UPDATED,
                     VALIDATION_FAILED, VALIDATION_NOT_EXECUTED, DockerfileResult, FromInstruction, ImageRef,
                     ImageRow, RepoResult, TechFinding)
from .registry_manager import RegistryClient, RegistryError, registry_label
from .tech_detector import assess_major_compat, detect_technologies, techs_for_dockerfile
from .validator import FAILED_V, NOT_EXECUTED, PASSED, Validator

log = get_logger("updater")


def read_text(path: Path) -> str:
    # newline="" keeps CRLF/LF exactly as-is; surrogateescape round-trips any non-UTF-8 bytes
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        return fh.read()


def write_text_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".diu-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class _Plan:
    """A change we intend to apply for one FROM instruction."""

    def __init__(self, fi: FromInstruction, new_image: str, row: ImageRow, other_rows: list[ImageRow]):
        self.fi, self.new_image, self.row, self.other_rows = fi, new_image, row, other_rows


class ImageUpdater:
    def __init__(self, settings: dict, registry: RegistryClient, validator: Validator):
        self.s = settings
        self.up = settings["upgrade"]
        self.registry = registry
        self.validator = validator
        self.report_only = bool(self.up["report_only"])

    # ------------------------------------------------------------------ repository
    def process_repository(self, repo_dir: Path, result: RepoResult) -> RepoResult:
        files = find_dockerfiles(repo_dir, self.up["max_dockerfile_depth"], self.up["ignore_dirs"])
        result.technologies = detect_technologies(repo_dir, ignore_dirs=self.up["ignore_dirs"])
        log.info("%s: %d Dockerfile(s), %d technology finding(s)", result.repo_name, len(files), len(result.technologies))
        for rel in files:
            try:
                result.dockerfiles.append(self.process_dockerfile(repo_dir, rel, result.technologies))
            except Exception as exc:  # noqa: BLE001 - one bad Dockerfile must never stop the rest
                log.exception("unexpected failure processing %s", rel)
                result.dockerfiles.append(DockerfileResult(path=rel, error=f"{type(exc).__name__}: {exc}"))
        return result

    # ------------------------------------------------------------------ Dockerfile
    def process_dockerfile(self, repo_dir: Path, rel: str, all_techs: list[TechFinding]) -> DockerfileResult:
        path = repo_dir / rel
        techs = techs_for_dockerfile(rel, all_techs)
        dres = DockerfileResult(path=rel, technologies=sorted({t.name for t in techs}))
        try:
            original = read_text(path)
        except OSError as exc:
            dres.error = f"cannot read file: {exc}"
            return dres
        parsed = parse_dockerfile(original)
        dres.from_count = len(parsed.froms)
        if not parsed.froms:
            dres.error = None
            dres.validation_details = "no FROM instructions found"
            return dres

        plans: list[_Plan] = []
        for fi in parsed.froms:
            try:
                rows, plan = self._evaluate(fi, parsed, techs, rel)
            except Exception as exc:  # noqa: BLE001
                log.exception("evaluation failed for %s FROM #%d", rel, fi.index)
                rows = [ImageRow(rel, fi.index, "-", fi.image_token, fi.image_token, "-", SAME_MAJOR, FAILED,
                                 f"Unexpected error: {type(exc).__name__}: {exc}")]
                plan = None
            dres.rows += rows
            if plan:
                plans.append(plan)

        if plans and not self.report_only:
            self._apply(repo_dir, rel, original, parsed, plans, dres, techs)
        self._finalize(dres)
        return dres

    # ------------------------------------------------------------------ evaluation
    def _row(self, rel, fi, ref: Optional[ImageRef], status, remarks, *, kind=SAME_MAJOR, new="-", old=None):
        return ImageRow(
            dockerfile=rel, instruction_index=fi.index,
            registry=registry_label(ref.registry) if ref else "-",
            image_name=ref.display_name if ref else (fi.resolved_image or fi.image_token or "-"),
            old_image=old if old is not None else (ref.raw if ref else (fi.resolved_image or fi.image_token or "-")),
            new_image=new, upgrade_type=kind, status=status, remarks=remarks)

    def _evaluate(self, fi: FromInstruction, parsed: ParsedDockerfile, techs, rel) -> tuple[list[ImageRow], Optional[_Plan]]:
        strategy = self.up["strategy"]
        if fi.is_stage_ref:
            return [self._row(rel, fi, None, SKIPPED, "References an earlier build stage (not an external image)")], None
        if fi.is_scratch:
            return [self._row(rel, fi, None, SKIPPED, "scratch base image - nothing to upgrade")], None
        if fi.resolved_image is None:
            return [self._row(rel, fi, None, SKIPPED, f"Cannot resolve image safely: {fi.unresolved_reason or 'unknown'}",
                              old=fi.image_token)], None
        try:
            ref = parse_image_ref(fi.resolved_image)
        except InvalidImageReference as exc:
            return [self._row(rel, fi, None, INVALID_IMAGE, str(exc), old=fi.resolved_image)], None

        arg_note = f" (via ARG {fi.arg_name})" if fi.arg_name else ""
        if ref.digest and self.up["digest_policy"] != "update":
            return [self._row(rel, fi, ref, SKIPPED, "Digest-pinned image; digest update policy is disabled")], None
        if not ref.tag:
            why = "Digest-only reference has no tag to evaluate" if ref.digest else "No tag (implicit 'latest') - nothing to compare"
            return [self._row(rel, fi, ref, NOT_SUPPORTED, why)], None
        cur = vm.parse_tag(ref.tag)
        if cur is None:
            return [self._row(rel, fi, ref, NOT_SUPPORTED, f"Tag '{ref.tag}' is not a versioned tag (cannot determine stable upgrade)")], None
        if not cur.is_stable:
            return [self._row(rel, fi, ref, SKIPPED, "Current tag is a pre-release/unstable tag")], None
        if cur.nums[0] > vm.MAX_SANE_MAJOR:
            return [self._row(rel, fi, ref, NOT_SUPPORTED, "Tag looks date/build-number based; semantic comparison not possible")], None

        depth = vm.line_depth(ref.repository, self.up)
        L = min(depth, len(cur.nums))
        parity = self.up.get("stable_major_parity", {}).get(vm.family_of(ref.repository))
        rows: list[ImageRow] = []
        plan_a: Optional[_Plan] = None
        plan_b: Optional[_Plan] = None

        # ---------------- Strategy A
        if strategy in {"same_major", "both"}:
            try:
                listing = self.registry.list_tags(ref, name_filter=vm.line_str(cur.nums[:L]) + ".")
                sel = vm.select_same_major(ref.tag, listing.tags, depth, self.up["precision"])
                row, plan_a = self._build_row(rel, fi, ref, sel, SAME_MAJOR, listing, arg_note)
            except RegistryError as exc:
                row = self._row(rel, fi, ref, SKIPPED, self._reg_reason(exc))
            rows.append(row)

        # ---------------- Strategy B
        if strategy in {"new_major", "both"}:
            try:
                listing = self.registry.list_tags(ref)
                sel = vm.select_new_major(ref.tag, listing.tags, depth, self.up["precision"], parity)
                row, plan_b = self._build_row(rel, fi, ref, sel, NEW_MAJOR, listing, arg_note)
                if plan_b:
                    new_line = vm.parse_tag(sel.tag).nums[:L]
                    ok, why = assess_major_compat(ref.repository, new_line, techs)
                    row.status = MAJOR_UPGRADE_RECOMMENDED if ok else REQUIRES_VALIDATION
                    row.remarks = ("Breaking changes possible. " + why).strip()
                    if not (self.up["auto_apply_major"] and ok and not self.report_only):
                        plan_b = None                      # proposal only
                    else:
                        row.status = UPDATE_AVAILABLE      # will become UPDATED when applied
                        row.remarks = "Automatic major upgrade enabled. " + why
            except RegistryError as exc:
                row = self._row(rel, fi, ref, SKIPPED, self._reg_reason(exc), kind=NEW_MAJOR)
            rows.append(row)

        # ARG edit unsafe -> report only, never guess
        if fi.arg_name and fi.arg_edit_blocked:
            for pl in (plan_a, plan_b):
                if pl:
                    pl.row.status = SKIPPED
                    pl.row.remarks = f"Update available but not applied: {fi.arg_edit_blocked}"
            return rows, None
        # one change per FROM: an applicable major upgrade supersedes the same-major one
        if plan_b and plan_a:
            plan_a.row.status = SKIPPED
            plan_a.row.remarks = "Superseded by automatic new-major upgrade"
        return rows, (plan_b or plan_a)

    def _reg_reason(self, exc: RegistryError) -> str:
        return {"auth": "Registry requires authentication / image is private",
                "not_found": "Repository not found in registry",
                "rate_limit": "Registry rate limit exceeded",
                "network": "Registry unreachable",
                "server": "Registry server error",
                "unsupported": "Registry API not supported"}.get(exc.kind, "Registry error") + f": {exc}"

    def _build_row(self, rel, fi, ref: ImageRef, sel: vm.Selection, kind: str, listing, arg_note: str):
        note = " (tag listing truncated; newest tags examined)" if not listing.complete else ""
        if sel.tag is None:
            if kind == NEW_MAJOR and "no newer stable major" in sel.reason:
                return self._row(rel, fi, ref, UP_TO_DATE, "Already on the newest stable major version" + note, kind=kind), None
            return self._row(rel, fi, ref, SKIPPED, sel.reason + note, kind=kind), None
        if sel.tag == ref.tag:
            return self._row(rel, fi, ref, UP_TO_DATE, "Already on latest stable version" + arg_note + note, kind=kind), None
        # prevent downgrade / cross-major mistakes defensively
        if vm.is_downgrade(ref.tag, sel.tag):
            return self._row(rel, fi, ref, SKIPPED, "Downgrade prevented", kind=kind), None
        if kind == SAME_MAJOR and not vm.same_major_line(ref.tag, sel.tag, vm.line_depth(ref.repository, self.up)):
            return self._row(rel, fi, ref, FAILED, "Internal safety check: selected tag crosses major line", kind=kind), None
        if self.s["registry"].get("verify_tag_exists", True):
            try:
                if not self.registry.tag_exists(ref, sel.tag):
                    return self._row(rel, fi, ref, FAILED, f"Selected tag '{sel.tag}' not found in registry", kind=kind), None
            except RegistryError as exc:
                return self._row(rel, fi, ref, SKIPPED, f"Could not verify tag exists: {exc}", kind=kind), None
        digest = None
        if ref.digest:   # digest policy == update
            try:
                digest = self.registry.get_digest(ref, sel.tag)
            except RegistryError as exc:
                return self._row(rel, fi, ref, SKIPPED, f"Could not resolve new digest: {exc}", kind=kind), None
        new_image = ref.render(tag=sel.tag, digest=digest)
        status = UPDATE_AVAILABLE
        remark = "Stable update" + arg_note
        if (kind == SAME_MAJOR and vm.parse_tag(ref.tag).nums == vm.parse_tag(sel.tag).nums
                and vm.parse_tag(ref.tag).build == vm.parse_tag(sel.tag).build):
            remark = "Pinned to fully specified tag" + arg_note
        row = self._row(rel, fi, ref, status, remark + note, kind=kind, new=new_image)
        return row, _Plan(fi, new_image, row, [])

    # ------------------------------------------------------------------ applying
    def _apply(self, repo_dir: Path, rel: str, original: str, parsed: ParsedDockerfile,
               plans: list[_Plan], dres: DockerfileResult, techs: list[TechFinding]) -> None:
        path = repo_dir / rel
        applied: list[_Plan] = []
        work = ParsedDockerfile(list(parsed.lines), parsed.froms, parsed.global_args, parsed.arg_lines, parsed.raw_text)
        for p in plans:
            try:
                work.lines = rewrite_from(work, p.fi, p.new_image)
                applied.append(p)
            except ValueError as exc:
                p.row.status = FAILED
                p.row.remarks = f"Could not rewrite safely: {exc}"
        if not applied:
            return
        new_text = "".join(work.lines)
        if new_text == original:
            return
        baseline = self.validator.baseline(repo_dir, rel)
        try:
            write_text_atomic(path, new_text)
            dres.modified = True
            vres = self.validator.validate_dockerfile(repo_dir, rel, new_text, techs, baseline)
        except BaseException:
            write_text_atomic(path, original)          # never leave a partially modified Dockerfile
            dres.modified = False
            raise
        dres.validation_status = vres.status
        dres.validation_details = "; ".join(vres.details + vres.not_executed_reasons)
        if vres.status == FAILED_V:
            write_text_atomic(path, original)
            dres.modified = False
            for p in applied:
                p.row.status = VALIDATION_FAILED
                p.row.remarks = "Reverted: " + (vres.details[-1] if vres.details else "validation failed")
                p.row.validation = "FAILED (original restored)"
            log.warning("%s: validation failed - original restored", rel)
            return
        if vres.status == NOT_EXECUTED:
            for p in applied:
                p.row.status = VALIDATION_NOT_EXECUTED
                p.row.remarks = "Applied; build validation NOT executed: " + "; ".join(vres.not_executed_reasons)
                p.row.validation = "SYNTAX OK; BUILD NOT EXECUTED"
        else:
            for p in applied:
                p.row.status = UPDATED
                p.row.validation = "PASSED"
        # sanity: re-parse and confirm only intended FROMs changed
        reparsed = parse_dockerfile(new_text)
        if len(reparsed.froms) != len(parsed.froms):
            write_text_atomic(path, original)
            dres.modified = False
            for p in applied:
                p.row.status = VALIDATION_FAILED
                p.row.remarks = "Reverted: FROM count changed after rewrite"
            dres.validation_status = FAILED_V

    # ------------------------------------------------------------------ outcomes
    def _finalize(self, dres: DockerfileResult) -> None:
        by: dict[int, list[ImageRow]] = {}
        for r in dres.rows:
            by.setdefault(r.instruction_index, []).append(r)
        for idx, rows in by.items():
            statuses = [r.status for r in rows]
            a = next((r for r in rows if r.upgrade_type == SAME_MAJOR), None)
            b = next((r for r in rows if r.upgrade_type == NEW_MAJOR), None)
            if any(s in APPLIED_STATUSES for s in statuses):
                out = next(s for s in statuses if s in APPLIED_STATUSES)
            elif any(s in FAILED_STATUSES for s in statuses):
                out = next(s for s in statuses if s in FAILED_STATUSES)
            else:
                out = (a or b).status
            dres.outcomes[idx] = out
            if b and b.status in {MAJOR_UPGRADE_RECOMMENDED, REQUIRES_VALIDATION}:
                dres.major_flags.add(idx)
        if self.report_only and not any(r.status == UPDATE_AVAILABLE for r in dres.rows) and not dres.rows:
            dres.validation_status = "NOT_RUN"
