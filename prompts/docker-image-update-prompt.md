# Docker Image Update Automation - Master Prompt (v2, updated to match the implemented project)

Use this prompt to re-generate, extend or audit the project with an AI coding assistant. It consolidates the two original
prompts (kept unchanged in `prompts/original/`) and reflects what the project actually does today, including the design
decisions that were made while building and testing it.

---

## Role

Act as a Senior Python Developer, DevOps Engineer, Docker Specialist and GitHub Actions Automation Expert.

## Goal

Maintain a production-ready, modular Python tool (target: latest stable Python, currently 3.15; minimum 3.11; dependencies pinned to the latest verified releases in `requirements.txt`) that, for many GitHub repositories listed in an Excel file:
scans for Dockerfiles, evaluates **every** `FROM` instruction against its registry, proposes (and, only when explicitly
enabled, applies) upgrades under two independent strategies, validates the result, writes per-repository and consolidated
reports, e-mails them, and runs unattended in GitHub Actions. **Safe by default**: report-only, no commits, no PRs, no
automatic major upgrades.

## Inputs

* `config/repositories.xlsx` - columns exactly `Repo_Name, Repo_Link, POC, Email, CC_Email`; one row per repository
  (public or private). Every row must appear in the consolidated CSV, even if it fails.
* `config/settings.yaml` - safe defaults; overridable by `DIU_*` environment variables (set by workflow inputs).
* Secrets/env (never hard-coded, never logged, never in reports/artifacts): `GITHUB_TOKEN` (stored as secret `GH_PAT` because
  GitHub forbids secrets starting with `GITHUB_`), `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`,
  optional `EMAIL_TO`/`EMAIL_CC` (only with `email.recipients_source: excel_plus_secrets`), optional `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`.

## Functional requirements

1. **Repository access** - clone (shallow) into the workspace folder `repos/{repo-name}/` (kept for the whole run; the code inside every `repos/{repo-name}/` is deleted only after all repos are done and the summary e-mail step finished, even if sending failed or test mode skipped it) with the token passed via git config *environment variables*
   (never argv/URL). Classify and report: auth failure, not found, permission denied, rate limit, network. Retry network/rate-limit
   errors with exponential back-off. Never modify unrelated files. Clean up temp dirs.
2. **Discovery** - `Dockerfile`, `Dockerfile.*`, `Dockerfile-*`, `*.Dockerfile`; root plus up to 4 sub-folder levels; skip `.git`,
   `node_modules`, `vendor`, virtualenvs, build output.
3. **Parsing** - line-based, Dockerfile-aware: continuations, comments (also inside continuations), parser directives, heredocs,
   `--platform`, `AS` aliases, multi-stage, multiple `FROM`, stage references, `scratch`, global `ARG` substitution (`$V`, `${V}`,
   `${V:-x}`, `${V:+x}`), digests, custom registries. Each `FROM` is evaluated independently. Unresolvable ARG / build-time
   variables -> `SKIPPED`, never guessed.
4. **Registries** - detect from the reference. Docker Hub (hub API + OCI fallback), GHCR, ECR Public, Google Artifact Registry/GCR,
   Azure ACR, MCR, Oracle, Quay, Red Hat, Kubernetes registries and any OCI registry via `/v2/.../tags/list` with Link pagination and
   the anonymous Bearer flow. Registry that cannot be queried -> `SKIPPED` with a reason. Verify the chosen tag exists (manifest HEAD).
5. **Strategies** (evaluated independently, selectable: same-major / new-major / both)
   * **A - same major**: newest stable tag in the current major line, same variant (`slim`, `alpine3.20`, `bookworm`, ...), prefer
     fully pinned tags, never downgrade, never cross a major line.
   * **B - new major**: newest stable tag in the newest higher major line, same variant. Detected dynamically at run time.
   * Major line = first N numeric components: N=1 by default, N=2 for python/golang/php/ruby/mysql (configurable). node: only even
     (LTS) majors. Exclude alpha/beta/rc/nightly/dev/preview/snapshot/canary; ignore date/build-number tags; build numbers such as
     `_9` in `17.0.9_9-jre` are not part of the variant.
6. **Major-upgrade safety** - majors are proposed, never applied by default. `MAJOR_UPGRADE_RECOMMENDED` only when the image family
   matches a detected technology *and* a declared runtime constraint allows the new major; otherwise `REQUIRES_VALIDATION`.
   `auto_apply_major` (default false) applies only the first kind, and only in a modifying run mode.
7. **Technology detection** - Node.js, React, Angular, Python, .NET, Go, Java Maven, Java Gradle, Java; multi-technology repos;
   declared runtime versions (`.nvmrc`, `engines`, `requires-python`, `go.mod`, `TargetFramework`, `global.json`, `pom.xml`/Gradle).
   Per-repository technology summary in the report. Dockerfile <-> technology scoping by directory.
8. **Modification** - token-level rewrite inside the `FROM` instruction only; preserve comments, CRLF/LF, indentation, flags,
   aliases, other instructions. For `FROM x:${V}` edit the ARG default only if the ARG is used nowhere else. Digest-pinned images
   untouched unless `digest_policy: update`.
9. **Validation** - atomic write -> `docker buildx build --check` (baseline-compared; static syntax-check fallback when buildx or the
   daemon is unavailable - an unavailable daemon is *not* a bad Dockerfile) -> optional `docker build` -> optional technology builds
   (only for detected technologies; opt-in). Any failure restores the original (`VALIDATION_FAILED`). Validation that could not run
   -> `VALIDATION_NOT_EXECUTED`, never a false success. Technology builds run on the runner toolchain, not in the new image.
10. **Git** - optional commit to a new branch and PR via REST API (`git.commit_changes`, `git.create_pull_requests`, both false by
    default; report-only forces them off).
11. **Reports** - per repository `dockerfile-reports/{repo-name}-{DD-Mon-YYYY}.txt` (ONE central folder in the workspace, created when missing, never inside a cloned repo, never overwritten for duplicate names, availability checked and logged but never fatal; archived as artifacts in Jenkins/Actions) (technology summary, detail table, validation table, summary);
    consolidated `Dockerfile-update-report-YYYY-MM-DD.csv` (16 required columns first, extension columns after; formula-injection
    safe); ZIP of repo reports; `overall-summary.txt`; per-repo logs. Statuses: UPDATED, SKIPPED, FAILED, UP_TO_DATE,
    MAJOR_UPGRADE_RECOMMENDED, REQUIRES_VALIDATION, NOT_SUPPORTED, INVALID_IMAGE, VALIDATION_FAILED + UPDATE_AVAILABLE,
    VALIDATION_NOT_EXECUTED. Counts are computed from real results; Dockerfile counts and `FROM`-instruction counts are distinct and
    mutually exclusive buckets.
12a. **Multiple tokens** - secrets `GIT_TOKEN`, `GIT_TOKEN2`, ... (explicitly mapped in the workflow); optional Excel column `Token_Name`
    names the token per repository (only names matching `GIT_TOKEN[n]` / legacy token names are accepted); empty = try all in order.
    The same token serves branch lookup, clone, push and PR. Log names only, never values; fail only the affected repo.
12b. **Branch selection** - `git ls-remote` then shallow-clone the first existing of `dev`, `development`, `develop`, `main` (configurable
    `git.branch_priority`), else the default branch; the PR targets that branch.
12c. **Test mode** (`DIU_TEST_MODE` / `testing.enabled` / workflow input `test_mode`) - run everything but do NOT send e-mail (save an `.eml`
    preview) and do NOT create pull requests.
12. **E-mail** - SMTP with STARTTLS/SSL (certificate verified), CSV + ZIP attached, summary body, start/end/duration.
    Recipients are 100% dynamic from the Excel file (no hard-coded addresses): `Email` -> TO (application owners), `CC_Email` -> CC
    (developers), across every row, including repos that failed. Cells may contain several addresses separated by comma, semicolon,
    space or newline. Trim, validate, skip empty/invalid ones (log them masked, e.g. `ab***`), de-duplicate globally (case- and
    whitespace-insensitive); an address in both columns stays in TO only, so no address appears twice. Send ONE consolidated e-mail
    after all repos are processed. Before sending log "Consolidated Email Notification Summary" (total repositories, unique TO, unique
    CC, duplicates removed, invalid skipped) and save it as `email-recipient-summary.txt`. Require >=1 valid TO; otherwise skip with a
    clear message. SMTP errors are handled gracefully, never fail the run, and reports remain available as workflow artifacts.
13. **Isolation** - a failure in one Dockerfile or repository never stops the rest. Exit code 0 unless the inventory is unreadable (2)
    or `exit.nonzero_on_repo_failure` is set (1).

## GitHub Actions requirements

* `.github/workflows/docker-image-update.yml`: `workflow_dispatch` inputs (repositories, strategy, dry_run [default true], run_mode,
  auto_apply_major, technology_validation, docker_build_validation, send_email) and a daily cron (UTC) with a *gate* job that applies
  `SCHEDULE_FREQUENCY` (daily/weekly/monthly) so frequency is configurable without code or YAML edits.
* Concurrency group (no overlap), `permissions: contents: read`, secrets only in step `env`, inputs only via `env` and validated
  against allow-lists before use, `timeout-minutes`, artifacts uploaded with `if: always()`.
* Current action majors verified at implementation time: `actions/checkout@v7`, `actions/setup-python@v7` (pip cache),
  `docker/setup-buildx-action@v4`, `actions/upload-artifact@v7`. Re-verify before changing.
* Dedicated dependency step (system packages, single `requirements.txt` (runtime + pytest, bounded to the current major), `pip check`), toolchain verification, self-tests before the run.
* Log scheduled slot, actual start, end and duration; GitHub cron is UTC (02:30 UTC = 08:00 IST).

## Testing requirements (mandatory, no overclaiming)

Syntax check every module; install in a clean venv; unit tests for version comparison/selection, prerelease filtering, variants,
parsing (single/multi-stage, ARG, heredoc, CRLF), registry handling (mocked HTTP: pagination, bearer flow, 401/403/404/429/5xx,
timeouts, back-off), technology detection for every stack, reports/CSV/counts, e-mail, workflow YAML; integration/E2E tests with real
local git repositories proving failure isolation and that dry-run modifies nothing. Use mocks where live services/credentials are
unavailable and **state clearly what was not tested live** (registries, real PAT clone, real PR, real SMTP, buildx with daemon,
technology builds, an actual Actions run).

## Deliverables

Source (`src/`), tests (`tests/`), `config/`, workflow, `requirements*.txt`, `.env.example`, `scripts/` (local run helpers, sample
generator), `prompts/`, `docs/`, `dockerfile-reports/` (example reports), `README.md` with step-by-step install / configure / run locally / run as a GitHub Action
locally (`act`) / run on GitHub / troubleshoot instructions.

## Known limitations to keep documented

Build-time `--build-arg` values unknown; only global ARGs resolvable; tag semantics heuristic; Docker Hub listings bounded by
`max_tag_pages`; no multi-arch check; no detection of existing PRs; authenticated registries (ACR/Oracle/Red Hat) reported as
`SKIPPED`; declared runtime constraints are minimums, so "recommended" is not "proven compatible".
