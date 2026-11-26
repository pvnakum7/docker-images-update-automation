"""Shared data models and status constants."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# --- Row / instruction statuses -------------------------------------------------
UPDATED = "UPDATED"
SKIPPED = "SKIPPED"
FAILED = "FAILED"
UP_TO_DATE = "UP_TO_DATE"
MAJOR_UPGRADE_RECOMMENDED = "MAJOR_UPGRADE_RECOMMENDED"
REQUIRES_VALIDATION = "REQUIRES_VALIDATION"
NOT_SUPPORTED = "NOT_SUPPORTED"
INVALID_IMAGE = "INVALID_IMAGE"
VALIDATION_FAILED = "VALIDATION_FAILED"
# Extensions to the required list (documented in README):
UPDATE_AVAILABLE = "UPDATE_AVAILABLE"            # report-only / dry-run: update exists, not applied
VALIDATION_NOT_EXECUTED = "VALIDATION_NOT_EXECUTED"

ALL_STATUSES = [
    UPDATED, SKIPPED, FAILED, UP_TO_DATE, MAJOR_UPGRADE_RECOMMENDED, REQUIRES_VALIDATION,
    NOT_SUPPORTED, INVALID_IMAGE, VALIDATION_FAILED, UPDATE_AVAILABLE, VALIDATION_NOT_EXECUTED,
]

FAILED_STATUSES = {FAILED, VALIDATION_FAILED, INVALID_IMAGE}
SKIPPED_STATUSES = {SKIPPED, NOT_SUPPORTED}
# Applied to the Dockerfile (VALIDATION_NOT_EXECUTED = changed, but build validation could not run)
APPLIED_STATUSES = {UPDATED, VALIDATION_NOT_EXECUTED}
# Something is available/proposed but nothing was changed
PENDING_STATUSES = {UPDATE_AVAILABLE, MAJOR_UPGRADE_RECOMMENDED, REQUIRES_VALIDATION}

SAME_MAJOR = "Same Major"
NEW_MAJOR = "New Major"


@dataclass
class ImageRef:
    """A parsed container image reference."""
    raw: str                      # reference exactly as written (after ARG resolution)
    registry: str = "docker.io"   # normalised registry host
    repository: str = ""          # e.g. library/node, org/app
    tag: Optional[str] = None
    digest: Optional[str] = None
    name_as_written: str = ""     # name part exactly as written (no tag / digest), used to re-render

    @property
    def display_name(self) -> str:
        """Short name for reports (library/ prefix dropped for Docker Hub)."""
        if self.registry == "docker.io":
            return self.repository.removeprefix("library/")
        return f"{self.registry}/{self.repository}"

    def render(self, tag: Optional[str] = None, digest: Optional[str] = None) -> str:
        """Re-render with the original name spelling (registry host, library/ prefix) preserved."""
        out = self.name_as_written or self.repository
        t = tag if tag is not None else self.tag
        if t:
            out += f":{t}"
        d = digest if digest is not None else self.digest
        if d:
            out += f"@{d}"
        return out


@dataclass
class FromInstruction:
    """One FROM instruction located in a Dockerfile."""
    index: int                     # 1-based ordinal within the file
    start_line: int                # 0-based index of first physical line
    end_line: int                  # 0-based index of last physical line (inclusive)
    platform: Optional[str]
    image_token: str               # raw token as written (may contain ${ARG})
    alias: Optional[str]
    resolved_image: Optional[str]  # token with ARGs resolved, or None if unresolvable
    unresolved_reason: Optional[str] = None
    is_stage_ref: bool = False
    is_scratch: bool = False
    # ARG-based references: the single variable whose default can be edited instead of the FROM line
    arg_name: Optional[str] = None
    arg_line: Optional[int] = None
    arg_prefix: str = ""
    arg_suffix: str = ""
    arg_edit_blocked: Optional[str] = None   # reason editing the ARG default is unsafe


@dataclass
class ImageRow:
    """One line of the repo report: a FROM instruction evaluated under one strategy."""
    dockerfile: str
    instruction_index: int
    registry: str
    image_name: str
    old_image: str
    new_image: str = "-"
    upgrade_type: str = SAME_MAJOR
    status: str = SKIPPED
    remarks: str = ""
    validation: str = "-"


@dataclass
class DockerfileResult:
    path: str
    technologies: list[str] = field(default_factory=list)
    from_count: int = 0
    rows: list[ImageRow] = field(default_factory=list)
    modified: bool = False
    validation_status: str = "NOT_RUN"   # PASSED / FAILED / NOT_EXECUTED / NOT_RUN
    validation_details: str = ""
    error: Optional[str] = None
    # Final per-instruction outcome (instruction_index -> status), used for exclusive counts
    outcomes: dict[int, str] = field(default_factory=dict)
    major_flags: set[int] = field(default_factory=set)

    def file_status(self) -> str:
        """Exclusive Dockerfile-level status. Priority: UPDATED > FAILED > SKIPPED > PENDING > UP_TO_DATE."""
        vals = list(self.outcomes.values())
        if self.modified and any(v in APPLIED_STATUSES for v in vals):
            return UPDATED
        if self.error or any(v in FAILED_STATUSES for v in vals):
            return FAILED
        if not vals or all(v in SKIPPED_STATUSES for v in vals):
            return SKIPPED
        if any(v in PENDING_STATUSES for v in vals):
            return UPDATE_AVAILABLE
        return UP_TO_DATE


@dataclass
class TechFinding:
    name: str
    indicators: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)       # e.g. "node>=20 (.nvmrc)"
    constraints: dict = field(default_factory=dict)       # {"min": (20,), "max": None, "source": ".nvmrc"}
    directory: str = "."


@dataclass
class RepoResult:
    repo_name: str
    repo_link: str
    poc: str = ""
    email: str = ""
    cc_email: str = ""
    dockerfiles: list[DockerfileResult] = field(default_factory=list)
    technologies: list[TechFinding] = field(default_factory=list)
    repo_error: Optional[str] = None
    pr_url: Optional[str] = None
    base_branch: Optional[str] = None      # branch that was analysed / is the PR target
    token_name: Optional[str] = None       # NAME of the token used (never its value)
    notes: list[str] = field(default_factory=list)
    report_path: Optional[str] = None

    # ---- counts (calculated from actual results) ----
    @property
    def total_dockerfiles(self) -> int:
        return len(self.dockerfiles)

    @property
    def total_from(self) -> int:
        return sum(d.from_count for d in self.dockerfiles)

    def _img(self, status_set) -> int:
        return sum(1 for d in self.dockerfiles for v in d.outcomes.values() if v in status_set)

    @property
    def updated_images(self) -> int:
        return self._img(APPLIED_STATUSES)

    @property
    def skipped_images(self) -> int:
        return self._img(SKIPPED_STATUSES)

    @property
    def failed_images(self) -> int:
        return self._img(FAILED_STATUSES)

    @property
    def up_to_date_images(self) -> int:
        return self._img({UP_TO_DATE})

    @property
    def pending_images(self) -> int:
        return self._img(PENDING_STATUSES)

    @property
    def major_recommendations(self) -> int:
        return sum(len(d.major_flags) for d in self.dockerfiles)

    def _files(self, status: str) -> int:
        return sum(1 for d in self.dockerfiles if d.file_status() == status)

    @property
    def updated_dockerfiles(self) -> int:
        return self._files(UPDATED)

    @property
    def skipped_dockerfiles(self) -> int:
        return self._files(SKIPPED)

    @property
    def failed_dockerfiles(self) -> int:
        return self._files(FAILED)

    @property
    def up_to_date_dockerfiles(self) -> int:
        return self._files(UP_TO_DATE)

    @property
    def pending_dockerfiles(self) -> int:
        return self._files(UPDATE_AVAILABLE)

    @property
    def overall_status(self) -> str:
        if self.repo_error:
            return "FAILED"
        if not self.dockerfiles:
            return "NO_DOCKERFILES"
        if self.failed_dockerfiles or self.failed_images:
            return "COMPLETED_WITH_ERRORS"
        return "SUCCESS"
