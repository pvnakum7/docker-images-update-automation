# Docker Image Update Automation

Automatically scans many GitHub repositories, finds every Dockerfile, checks each `FROM` image against its container registry,
proposes (and optionally applies) safe upgrades, validates the result, writes reports, and e-mails them - locally or on a schedule in GitHub Actions.

> **Safe by default.** Out of the box it is *report-only*: it never modifies a Dockerfile, never commits, never opens a pull request
> and never applies a major-version upgrade. You must opt in explicitly to each of those.

## Table of contents
1. [Overview and purpose](#1-overview-and-purpose) · 2. [Features](#2-features) · 3. [Prerequisites](#3-prerequisites) · 4. [Project structure](#4-project-structure)
5. [Environment variables, secrets and permissions](#5-environment-variables-secrets-and-permissions) · 6. [Installation and configuration](#6-installation-and-configuration)
7. [Run locally](#7-run-locally) · 8. [Test the GitHub Action locally (act)](#8-test-the-github-action-locally-with-act) · 9. [Run on GitHub Actions](#9-run-on-github-actions)
10. [Troubleshooting](#10-troubleshooting) · 11+. Design reference, settings, reports, testing, limitations

## 1. Overview and purpose
Keeping base images current across dozens of repositories is manual and error-prone. This tool reads an Excel inventory
(`Repo_Name, Repo_Link, POC, Email, CC_Email`), processes each repository independently, and answers for every `FROM` line:
*is there a newer stable tag, is it safe, and what would change?* It produces a text report per repository, one consolidated CSV, and an
e-mail to your team. With explicit opt-in it can also edit the Dockerfiles, validate them, and open pull requests.

## 2. Features
* Public and private GitHub repositories (token in an environment variable / GitHub secret, never hard-coded or logged).
* Finds `Dockerfile`, `Dockerfile.dev/.prod/...`, `*.Dockerfile` at the root and up to 4 levels deep; skips `node_modules`, `.git`, `vendor`, venvs.
* Multi-stage, multi-`FROM`, `AS` aliases, `--platform`, `ARG`-based images, digests, custom registries; every `FROM` evaluated independently.
* Registries: Docker Hub, GHCR, ECR Public, Google Artifact Registry/GCR, MCR, Quay, Red Hat, Kubernetes registries, any OCI registry (private ones are reported as skipped).
* Two independent strategies: **same major** (latest stable patch/minor, variant preserved) and **new major** (latest stable major, never auto-applied by default).
* Technology detection (Node.js, React, Angular, Python, .NET, Go, Java Maven/Gradle) used to judge whether a major upgrade is plausible.
* Surgical Dockerfile edits (only the `FROM` token), validation with `docker buildx build --check`, automatic restore on failure.
* Reports: per-repo `.txt`, consolidated CSV, ZIP, overall summary, per-repo logs; HTML-free, spreadsheet-safe.
* SMTP e-mail with attachments; optional branch + pull request creation.
* Failure isolation: one bad Dockerfile/repository never stops the run.
* GitHub Actions workflow: manual inputs, configurable daily/weekly/monthly schedule, concurrency control, artifacts.

## 3. Prerequisites
| Need | Version | Used for |
|---|---|---|
| Python | **3.15** recommended (latest stable 3.15.2); 3.11+ supported | the tool |
| Git | 2.31+ | cloning (token passed via `GIT_CONFIG_*` env) |
| Docker + Buildx | optional | `buildx build --check`, optional `docker build`; without them a built-in syntax check is used |
| Docker Desktop | only for section 8 | running the workflow locally with `act` |
| `act` | optional | https://github.com/nektos/act |
| GitHub PAT | for private repos / PRs | see section 5 |
| SMTP account | for e-mail | any provider supporting STARTTLS (587) or SSL (465) |

Python packages (`requirements.txt`, always the latest release within the tested major version): PyYAML, openpyxl, requests, plus pytest for the self-tests - one single file, no separate dev requirements.

## 4. Project structure
```
Docker-Image-automation/
|-- .github/
|   |-- workflows/docker-image-update.yml       GitHub Actions workflow (manual + scheduled)
|   `-- dependabot.yml                          weekly dependency / action update PRs
|-- Jenkinsfile                                 declarative Jenkins pipeline (archives dockerfile-reports/)
|-- config/
|   |-- repositories.xlsx                       INPUT: your repositories + POC details (edit this)
|   `-- settings.yaml                           all behaviour switches (safe defaults)
|-- src/                                        application code (one module per concern, see section 11)
|-- tests/                                      231 pytest tests (+ tests/act/ event payloads for local workflow runs)
|-- scripts/
|   |-- run_local.ps1 / run_local.sh            one-command local run (creates venv, installs, loads .env)
|   `-- generate_samples.py                     regenerates the example reports in docs/dockerfile-reports (mocked data)
|-- prompts/
|   |-- docker-image-update-prompt.md           CURRENT master prompt (v2)
|   `-- original/                               the two original prompts, unchanged
|-- docs/
|   |-- QUICKSTART.md                           short start guide
|   `-- dockerfile-reports/                     EXAMPLE reports (documentation only, mocked data)
|-- dockerfile-reports/                         OUTPUT (created at run time): ONE {repo-name}-{DD-Mon-YYYY}.txt per repository
|-- repos/                                      WORKING: repositories are cloned to repos/{repo-name}/ and emptied after the e-mail
|-- reports/                                    OUTPUT (git-ignored): CSV, ZIP, overall summary, logs
|-- .env.example   .act-secrets.example   .actrc   .gitignore   .python-version
`-- requirements.txt  README.md
```

## 5. Environment variables, secrets and permissions
Locally, put variables in `.env` (copy `.env.example`). On GitHub they are **Actions secrets / variables**.

| Name | Where | Required | Purpose |
|---|---|---|---|
| `GIT_TOKEN`, `GIT_TOKEN2`, `GIT_TOKEN3` ... `GIT_TOKEN10` | secret | private repos / PRs | **several GitHub tokens** (e.g. one per org/account). The Excel column `Token_Name` picks one per repository, see *Multiple GitHub tokens* below |
| `DIU_TEST_MODE` | variable/input | testing | `true` = run everything but **no e-mail and no pull requests** (workflow input `test_mode`, repo variable `TEST_MODE` for scheduled runs) |
| `GITHUB_TOKEN` (secret name **`GH_PAT`** on GitHub), legacy single token | secret | private repos / PRs | PAT used to clone and open PRs. GitHub forbids secret names starting with `GITHUB_`, so the workflow maps `GH_PAT` -> `GITHUB_TOKEN` |
| `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | secret | e-mail | Port 465 = implicit TLS, otherwise STARTTLS |
| `EMAIL_FROM` | secret | e-mail | sender address |
| `EMAIL_TO`, `EMAIL_CC` | secret (**optional**) | e-mail | ignored by default - recipients come from the Excel file. Only merged in when `email.recipients_source: excel_plus_secrets` |
| `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` | secret | optional | higher Docker Hub rate limits |
| `SCHEDULE_FREQUENCY` / `SCHEDULE_WEEKDAY` / `SCHEDULE_DAY_OF_MONTH` | variable | optional | `daily|weekly|monthly`, 1-7, 1-28 (default weekly, Monday) |
| `SCHEDULED_STRATEGY` / `SCHEDULED_DRY_RUN` / `SCHEDULED_MODE` | variable | optional | defaults `same_major` / `true` / `report_only` |
| `DIU_*` | env | optional | `DIU_STRATEGY, DIU_REPORT_ONLY, DIU_DRY_RUN, DIU_AUTO_MAJOR, DIU_CREATE_PR, DIU_COMMIT, DIU_REPOSITORIES, DIU_TECH_VALIDATION, DIU_DOCKER_BUILD, DIU_EMAIL_ENABLED, DIU_REPOS_FILE` override `settings.yaml` |

**Create the GitHub PAT** (GitHub -> Settings -> Developer settings -> Personal access tokens -> *Fine-grained tokens*): choose *Only select repositories*
(the ones in your Excel file), then permissions: **Metadata: Read** and **Contents: Read** for report-only. For update + pull request add
**Contents: Read and write** and **Pull requests: Read and write**. If your organisation uses SAML SSO, authorise the token for it.
The workflow itself needs only `contents: read` on the tooling repository.

## 6. Installation and configuration
**Windows (PowerShell)**
```powershell
cd C:\Users\Imagine\Documents\Automation\Docker-Image-automation
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process Bypass
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pytest -q                     # expect: 176 passed
```
**Linux / macOS / WSL**
```bash
cd Docker-Image-automation && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && python -m pytest -q
```
Configure:
1. Open `config/repositories.xlsx` and replace the sample rows. Columns: `Repo_Name, Repo_Link, POC, Email, CC_Email` and the optional `Token_Name` (a Google Sheet must be downloaded as `.xlsx`; the tool reads the Excel file)
   (`Repo_Link` like `https://github.com/org/repo`; several CC addresses separated by commas).
2. Review `config/settings.yaml`. The defaults are safe (report-only). Every key is commented.
3. Copy `.env.example` to `.env` and fill in the values you need (token for private repos; SMTP only if you want e-mail).

## 7. Run locally
Easiest (creates the venv, installs, loads `.env`, runs report-only without e-mail):
```powershell
.\scripts\run_local.ps1                      # Windows
./scripts/run_local.sh                        # Linux/macOS/WSL
```
Manual:
```powershell
$env:GITHUB_TOKEN = "ghp_..."                 # only for private repos
python -m src.main --config config/settings.yaml --no-email
python -m src.main --repos-file config\repositories.xlsx --output-dir out --no-email
$env:DIU_STRATEGY = "both"; $env:DIU_REPOSITORIES = "repo-a,repo-b"; python -m src.main --no-email   # strategies / subset
```
Linux/macOS: `export DIU_STRATEGY=both` instead of `$env:`.
Afterwards look at `dockerfile-reports/<repo-name>-<DD-Mon-YYYY>.txt` (one per repository) and in `reports/`: `Dockerfile-update-report-<date>.csv`, `overall-summary.txt`, `logs/`.

**Actually modifying Dockerfiles** (only when you are sure): set `upgrade.report_only: false` in `settings.yaml` (or `DIU_REPORT_ONLY=false`) **and** `git.commit_changes: true`
(otherwise edits happen only in a throw-away clone and are flagged in the report). Add `git.create_pull_requests: true` for PRs (needs the write permissions above).
`auto_apply_major: true` additionally allows major upgrades whose compatibility is established; leave it `false` unless you review every PR.

Try it with no network or token against a local repo: `python -m pytest tests/test_e2e_main.py -q` runs the whole pipeline on temporary git repositories.

## 8. Test the GitHub Action locally with `act`
`act` runs the workflow in Docker containers on your machine. *Note: this procedure is documented but was not executed while building the
project - if a step differs on your machine, see the notes below.*
1. Install Docker Desktop (running) and `act` (`winget install nektos.act`, `choco install act-cli`, or `brew install act`).
2. `copy .act-secrets.example .act-secrets` and fill the values (it is git-ignored). Use `GH_PAT` (not `GITHUB_TOKEN`).
3. From the project folder (`.actrc` selects a full Ubuntu image and amd64):
```powershell
act workflow_dispatch -W .github/workflows/docker-image-update.yml -e tests/act/workflow_dispatch.json --secret-file .act-secrets --artifact-server-path .\.act-artifacts
act schedule -W .github/workflows/docker-image-update.yml -e tests/act/schedule.json -j gate     # only checks the schedule gate logic
act -l                                                                                            # list jobs
```
Notes: `workflow_dispatch.json` runs a **dry run, report-only, no e-mail**. The first run downloads a large image. Under `act` the
`docker/setup-buildx-action` and `upload-artifact` steps can be unreliable; if so, run the Python part directly (section 7) - it is
exactly what the workflow's *Run Docker image update* step executes - and validate the workflow syntax with `python -m pytest tests/test_workflow.py`.
Scheduled-run date logic can be checked with `SCHEDULE_FREQUENCY` supplied via `--var SCHEDULE_FREQUENCY=daily`.

## 9. Run on GitHub Actions
1. **Create a repository** (private recommended) and push this folder to it (`git init`, `git add .`, `git commit`, `git remote add origin ...`, `git push -u origin main`).
   `config/repositories.xlsx` goes with it, so keep the repository private (it contains contact e-mails).
2. **Add secrets**: repo -> Settings -> Secrets and variables -> Actions -> *Secrets* -> New repository secret: `GH_PAT`, `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM` (+ optional `EMAIL_TO`/`EMAIL_CC`, Docker Hub ones).
3. **Optional variables** (same page, *Variables* tab): `SCHEDULE_FREQUENCY`, `SCHEDULE_WEEKDAY`, `SCHEDULE_DAY_OF_MONTH`, `SCHEDULED_STRATEGY`, `SCHEDULED_DRY_RUN`, `SCHEDULED_MODE`.
4. **Allow Actions**: Settings -> Actions -> General -> allow actions and reusable workflows.
5. **First run - manual dry run**: Actions tab -> *Docker Image Update* -> *Run workflow*. Keep `dry_run = true`, pick `upgrade_strategy` (`both` shows everything), choose a couple of repositories in the `repositories` box for a first test, then *Run*.
6. **Read the result**: job log (ends with *Overall Dockerfile Update Report*), the **job summary**, the e-mail, and the artifact `docker-image-update-reports-<run id>` (reports, CSV, ZIP, logs; kept 30 days).
7. **Real updates**: run again with `dry_run = false` and `run_mode = update_and_create_pr` (PAT needs write permissions). Review each PR; majors appear only when `auto_apply_major` is enabled and compatibility is established.
8. **Scheduling**: the workflow wakes daily at 02:30 UTC (08:00 IST); the *gate* job runs the real job only when due per `SCHEDULE_FREQUENCY`. Scheduled runs are dry-run/same-major unless you set the `SCHEDULED_*` variables. To change the hour, edit the `cron:` line (UTC). GitHub can delay scheduled runs and pauses them after 60 days of repository inactivity.

## 10. Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: yaml/openpyxl` | venv not active or deps not installed: `pip install -r requirements.txt` |
| PowerShell: running scripts is disabled | `Set-ExecutionPolicy -Scope Process Bypass` then re-run |
| `inventory is missing required column(s)` | header row must be exactly `Repo_Name, Repo_Link, POC, Email, CC_Email` |
| Repo `not_found` | wrong link, or PAT has no access to that private repo (GitHub reports both as "not found") |
| Repo `auth: authentication failed` | PAT expired/revoked or SSO not authorised for the org |
| `permission: ...` | PAT lacks Contents (read) - or write for PR mode |
| All images `SKIPPED ... Registry unreachable` | firewall/proxy blocks registry hosts; set `HTTPS_PROXY`/`SSL_CERT_FILE` if your company proxy requires it |
| `Registry requires authentication` | private/authenticated registry (ACR, Oracle, ...): not supported, reported as skipped by design |
| `rate limit` remarks | add `DOCKERHUB_*` credentials; reduce frequency or repo count |
| `buildx unavailable ... used static syntax check` | Docker/daemon not running; harmless, but only a basic syntax check ran. Start Docker Desktop for `buildx --check` |
| Update shows `UPDATE_AVAILABLE` but file unchanged | report-only is on (default). See section 7 to enable modification |
| `UPDATED` but nothing in the repo | modification without `commit_changes` only edits the clone in `repos/<repo-name>/` (deleted at the end of the run); enable commit/PR |
| Row `SKIPPED ... ARG X is referenced N times` | the ARG is used elsewhere; edit it manually (tool refuses to guess) |
| `VALIDATION_NOT_EXECUTED` | a requested build tool/credential was missing on the runner; see the remark |
| `no valid TO recipient` | no row has a valid address in the `Email` column (check the `Invalid email addresses skipped` log line); reports are still saved as artifacts |
| `token GIT_TOKENn is not configured` | the sheet names a token whose secret is missing/empty; add the secret and map it in the workflow `env` (GIT_TOKEN..GIT_TOKEN10 are pre-mapped) |
| `invalid Token_Name` | only `GIT_TOKEN`, `GIT_TOKEN2`, `GIT_TOKEN3` ... (and legacy `GITHUB_TOKEN`/`GH_TOKEN`) are accepted - a sheet cannot read other secrets |
| Wrong branch analysed | check `Branch Analysed:` in the repo report; order is `git.branch_priority` (dev, development, develop, main, then the default branch) |
| No e-mail | `SMTP_*`/`EMAIL_*` not all set, or `DIU_EMAIL_ENABLED=false` / `--no-email`; the log's `Email:` line says why. Gmail needs an app password |
| Workflow: *Context access might be invalid: GITHUB_TOKEN secret* | you created a secret named `GITHUB_TOKEN`; name it `GH_PAT` |
| Scheduled run did nothing | the gate decided it was not due (see `SCHEDULE_FREQUENCY`), or the repo was inactive for 60 days |
| Two runs queued | by design (`concurrency`): a new run waits for the active one |
| Need more detail | logs are in `reports/logs/<repo>.log`; set `output.log_level: DEBUG` |

## 11. Architecture (design reference)

```
GitHub Actions (cron / manual)
        │
        ▼
   src/main.py ── reads config/settings.yaml (+ DIU_* env overrides from workflow inputs)
        │          reads config/repositories.xlsx  (Repo_Name, Repo_Link, POC, Email, CC_Email)
        ▼
 for each repo  (fully isolated: any exception is caught and recorded, the loop continues)
   github_manager   clone (token via env, never argv) ─────────────► repos/<repo-name>/ (emptied after the summary e-mail)
   dockerfile_scanner  Dockerfile, Dockerfile.*, *.Dockerfile  (≤ 4 levels, skips .git/node_modules/vendor/venv…)
   tech_detector    Node/React/Angular/Python/.NET/Go/Maven/Gradle/Java  (+ declared runtime versions)
   image_updater    for every FROM (independently):
        dockerfile_parser   parse → resolve ARG → ImageRef
        registry_manager    tags / manifests / digests via the right API
        version_manager     Strategy A (same major)  +  Strategy B (new major)
        compatibility       tech_detector.assess_major_compat → MAJOR_UPGRADE_RECOMMENDED | REQUIRES_VALIDATION
        apply (only if allowed) → validator → restore original on failure
   github_manager   (optional) branch + commit + pull request
        │
        ▼
 report_generator   dockerfile-reports/<repo>-<DD-Mon-YYYY>.txt ×N,  Dockerfile-update-report-YYYY-MM-DD.csv,  ZIP
 email_sender       SMTP (STARTTLS/SSL) with CSV + ZIP attached, summary in body
 main               overall summary → Actions log + job summary + reports/overall-summary.txt
```

| Module | Responsibility |
|---|---|
| `config.py` | YAML settings with safe defaults, `DIU_*` env overrides, safety invariants (report-only ⇒ no commit/PR) |
| `github_manager.py` | Excel inventory, clone with token, error classification, commit/push, PR via REST API, retries/back-off |
| `dockerfile_scanner.py` | Dockerfile discovery |
| `dockerfile_parser.py` | Line-based Dockerfile-aware parser (continuations, comments, heredocs, ARG, digests) and **surgical** rewriter |
| `registry_manager.py` | Registry identification, anonymous bearer-token flow, tag listing, manifest HEAD, digests, retry/backoff/rate-limit |
| `version_manager.py` | Tag parsing, stable filtering, Strategy A / B selection |
| `tech_detector.py` | Technology detection and image↔technology compatibility rules |
| `image_updater.py` | Orchestration per Dockerfile; apply → validate → restore |
| `validator.py` | `docker buildx build --check` (or static check), optional `docker build`, opt-in technology builds |
| `report_generator.py` | Text reports, CSV (formula-injection safe), overall summary, ZIP |
| `email_sender.py` | SMTP delivery |
| `logger.py` | Structured logging, secret redaction, per-repo log files |

## 12. How version selection works

A tag is split into `prefix` (`v`), numeric components, and a **variant** suffix (`-slim`, `-alpine3.20`, `-bookworm`…).

* **Variant is matched exactly**, so `slim` never becomes `alpine`, and `1.27-alpine` is never moved to `1.29-alpine3.20`.
  A leading build number (`17.0.9_9-jre`) is not part of the variant.
* **Stable only**: tags with `alpha, beta, rc, nightly, dev, preview, snapshot, canary, edge, test, …` tokens are dropped.
  Date/build-number tags (first component > 999) are ignored. Non-versioned tags (`latest`, `lts-slim`) → `NOT_SUPPORTED`.
* **Strategy A – same major**: newest stable tag in the current *major line*, same variant, never a downgrade.
* **Strategy B – new major**: newest stable tag in the newest *higher* major line, same variant.
* **Major line** = first N numeric components. N = 1 normally (node 22, postgres 16), but **2 for python, golang, php, ruby, mysql**
  (`python 3.12 → 3.13` and `go 1.22 → 1.23` carry the breaking changes). Configurable in `settings.yaml`.
* **node**: only even majors are proposed (odd releases are not LTS). Configurable (`stable_major_parity`).
* **Precision**: `full` (default) prefers a fully pinned tag (`22.24.0-slim` over `22-slim`); `match_current` keeps the existing precision.
* The chosen tag is verified to exist with a manifest `HEAD` before it is proposed.

## 13. How unsafe major upgrades are prevented

1. Major upgrades are **proposed, not applied**, unless `auto_apply_major: true` *and* run mode allows modification.
2. Even then a major is applied only when compatibility is **established**: the image family maps to a detected technology
   *and* a declared runtime constraint (`.nvmrc`, `package.json engines`, `requires-python`, `go.mod`, `<TargetFramework>`,
   `global.json`, `pom.xml`/Gradle Java version) allows the new major. Otherwise the row is `REQUIRES_VALIDATION`.
3. Established-but-not-applied ⇒ `MAJOR_UPGRADE_RECOMMENDED`.
4. In same-major mode a cross-major result is impossible by construction and re-checked defensively (`FAILED` safety check).
5. Web servers (nginx/httpd/caddy) and unmapped images (alpine, postgres, redis…) always need manual validation for majors.

## 14. How Dockerfile changes are validated

Before: original content kept in memory; exact `FROM` located; new tag verified; downgrade/prerelease/variant checks.
Rewrite is **token-level inside the `FROM` instruction only** (never a global replace), preserving comments, CRLF/LF, flags
(`--platform`), aliases, indentation and every other line. For `FROM node:${V}` the **ARG default** is edited, but only
when the ARG is used nowhere else; otherwise the row is `SKIPPED` with the reason (nothing is guessed).

After: write atomically → `docker buildx build --check` (baseline-compared so pre-existing lint warnings are not blamed on the
change). If Docker/buildx/daemon is unavailable a built-in static syntax check is used and the report says so.
Optional `docker build`, optional technology build commands. **Any failure ⇒ original restored** (`VALIDATION_FAILED`), and a
`try/finally` guarantees no partially modified file. If validation was requested but could not run (missing tool, private feed,
missing credentials) the status is `VALIDATION_NOT_EXECUTED`, never a false `UPDATED`.

> Technology build checks (`npm run build`, `mvn validate`, …) run on the *runner's* toolchain: they confirm the application
> still builds, they do **not** build inside the new base image. Only `validation.docker_build` exercises the new image.

## 15. Registry support and differences

| Registry | Method |
|---|---|
| Docker Hub | `hub.docker.com` tags API (newest first, name filter, pagination); falls back to OCI `/v2/…/tags/list` |
| GHCR, Quay.io, ECR Public, Google Artifact Registry/GCR, MCR, registry.k8s.io, Red Hat, any OCI registry | OCI Distribution API `/v2/<name>/tags/list` with `Link` pagination and anonymous Bearer-token flow |
| Azure ACR, Oracle Container Registry, private images | Anonymous access usually refused ⇒ image is `SKIPPED` with “requires authentication” |

Any registry that cannot be queried (auth, not found, rate limit, network, unsupported API) ⇒ `SKIPPED` with an explanatory remark;
retries use exponential back-off and honour `Retry-After`. Optional `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` secrets raise Docker Hub limits.

## 16. Settings (`config/settings.yaml`)

Every key is documented inline. Highlights: `upgrade.strategy` (`same_major|new_major|both`), `upgrade.report_only` (**true**),
`upgrade.auto_apply_major` (**false**), `upgrade.max_dockerfile_depth` (4), `upgrade.digest_policy` (`skip`), `registry.timeout_seconds`,
`registry.retry_count`, `validation.*`, `git.commit_changes` (**false**), `git.create_pull_requests` (**false**), `email.enabled`,
`output.reports_dir`. Environment overrides: `DIU_STRATEGY, DIU_REPORT_ONLY, DIU_DRY_RUN, DIU_AUTO_MAJOR, DIU_CREATE_PR, DIU_COMMIT,
DIU_REPOSITORIES, DIU_TECH_VALIDATION, DIU_DOCKER_BUILD, DIU_EMAIL_ENABLED, DIU_REPOS_FILE`.

## 17. Reports, statuses and counting rules

Per repository: `dockerfile-reports/<repo-name>-<DD-Mon-YYYY>.txt`, e.g. `dockerfile-reports/repo-one-22-Nov-2026.txt` (technology summary, detail table, validation table, summary).
(They are written to the output folder, not into the cloned repository, so the target repositories stay untouched; set
Consolidated: `reports/Dockerfile-update-report-YYYY-MM-DD.csv` (one row per Excel row, failed repositories included),
`reports/Docker-repository-reports-YYYY-MM-DD.zip`, `reports/overall-summary.txt`, `reports/logs/<repo>.log`.

Statuses: the ten required (`UPDATED SKIPPED FAILED UP_TO_DATE MAJOR_UPGRADE_RECOMMENDED REQUIRES_VALIDATION NOT_SUPPORTED INVALID_IMAGE VALIDATION_FAILED`)
**plus two extensions**: `UPDATE_AVAILABLE` (update exists but report-only/dry-run) and `VALIDATION_NOT_EXECUTED`.

* A `FROM` can have two report rows (one per strategy). Counts are **per FROM instruction**, mutually exclusive:
  Updated / Failed / Skipped / Up-to-date / Pending. `Major_Upgrade_Recommendations` counts instructions with a proposed major
  (`MAJOR_UPGRADE_RECOMMENDED` or `REQUIRES_VALIDATION`) and is independent of those buckets.
* **Dockerfile** counts are mutually exclusive with priority *Updated > Failed > Skipped > Pending > Up-to-date*
  (a file with one updated and one failed image counts as Updated; the failed image still appears in Failed Images).
* A repository-level failure (clone/auth/not found/invalid row) ⇒ `Overall_Status = FAILED` with the reason in `Error`.
  Run status: `SUCCESS`, `PARTIAL_SUCCESS`, `FAILED`. The CSV appends extension columns after the 16 required ones.

## 18. Testing – what was and was not verified

Run `python -m pytest -q` (176 tests, ~5 s, no network/Docker/credentials needed): version selection and prerelease filtering, variants
(slim/alpine/build-number), parser (single/multi-stage, ARG, heredoc, CRLF, digests, custom registries), surgical rewrite, registry client
against a mocked HTTP layer (pagination, bearer flow, 401/403/404/429/5xx/timeout, backoff), technology detection for every listed stack,
reports/CSV/summary counts, e-mail building/attachments/TLS mode, workflow YAML structure, **end-to-end runs against real local git
repositories** (failure isolation, dry-run leaves repos untouched, commit to a branch only touches Dockerfiles).

**Not verified (needs your environment):** live registry APIs (sandbox had no outbound registry access), live GitHub clone with a real PAT,
real PR creation, real SMTP delivery, `docker buildx build --check` against a running daemon, technology build commands, and an actual
GitHub Actions run (workflow was validated statically: YAML parse, structure assertions, `bash -n` of every script; `actionlint` was not run).
See *Limitations*.

## 19. Limitations

* `--build-arg` values supplied at build time are unknown; `ARG` defaults are used, ARGs without defaults ⇒ `SKIPPED`.
* Only global `ARG`s declared before the first `FROM` are resolvable; `FROM` using `$BUILDPLATFORM`/`$TARGETARCH` is `SKIPPED`.
* Tag semantics are heuristic. Distros/versions embedded in variants (`alpine3.20`) must match exactly, so those are only moved within
  the same distro version; images with non-semver tagging (calendar versions, `latest`, `lts`) are `NOT_SUPPORTED`.
* Docker Hub listings are bounded by `registry.max_tag_pages`; truncation is noted in the remarks.
* Multi-arch availability for a `--platform` is not checked.
* Applying updates without `commit_changes` only changes a throw-away clone (flagged in the report). Existing open PRs from earlier runs are not detected.
* Declared runtime constraints are usually *minimums* (`>=3.9`, `java 17`), so `MAJOR_UPGRADE_RECOMMENDED` means "nothing declared forbids it", **not** "proven compatible" - always run the full build/tests before merging a major.
* Odd/LTS rules exist only for node; other families’ support policies are not modelled.
* Azure ACR / Oracle / Red Hat authenticated registries are out of scope (no credentials support) and are reported as `SKIPPED`.


## Custom Dockerfile names, several tokens, branch priority, test mode

**Dockerfile names.** Every file named `Dockerfile`, `Dockerfile.<anything>` (`Dockerfile.dev`, `Dockerfile.stage`), `Dockerfile-<x>`,
`Dockerfile_<x>` or `<x>.Dockerfile` is found, in the repository root and up to 4 folder levels down. So
`Dockerfile`, `frontend/Dockerfile`, `frontend/Dockerfile.dev` and `backend/Dockerfile.stage` are all processed
(`Dockerfile.bak`, `.md`, `.txt` ... are ignored). Covered by a test.

**Multiple GitHub tokens.** Add secrets `GIT_TOKEN`, `GIT_TOKEN2`, `GIT_TOKEN3` ... (up to `GIT_TOKEN10` are already mapped in the
workflow; for more, add lines to the *Run Docker image update* step `env`). In the Excel file add the column **`Token_Name`** and put the
secret *name* in it (`GIT_TOKEN2`). That one token is used for the whole chain: branch lookup -> clone -> push -> pull request.
`Token_Name` empty = every configured token is tried in order until one has access. A named token that is not configured fails only
that repository. Only token *names* are logged/reported (`GitHub Token Used: GIT_TOKEN2`), never values.

**Branch priority.** The branch that is analysed (and that the PR targets) is the first that exists on the remote:
`dev` -> `development` -> `develop` -> `main`; none present -> the repository default branch (noted in the report).
Change with `git.branch_priority` or `DIU_BRANCH_PRIORITY`. `develop` was placed after `development`, before `main`; reorder in settings if you want it elsewhere.

**Test mode.** `DIU_TEST_MODE=true` (workflow input `test_mode`, default **true** for manual runs; scheduled runs use repository variable
`TEST_MODE`, default true) runs everything except the two side effects. If you prefer to disable them by hand in the code:

| Side effect | Function | Where to comment out |
|---|---|---|
| Send e-mail | `email_sender.send_report(...)` | `src/main.py`, in `run()`, the line marked `# <-- EMAIL SENDING` |
| Create pull request | `GitHubManager.create_pull_request(...)` | `src/main.py`, in `_commit_and_pr()`, the line marked `# <-- PULL REQUEST CREATION` |

(`commit_and_push` only runs when `git.commit_changes` is true, which is off by default and forced off in report-only mode.)
In test mode the e-mail is built and saved as `email-preview-NOT-SENT.eml` and the recipient summary is still logged.
Set `test_mode = false` (and the `TEST_MODE` variable to `false`) to go live.


## Where repositories are cloned, and where reports go

* **Clones** - every repository is shallow-cloned (`--depth 1`, selected branch only) into the workspace at
  `repos/{repo-name}/` (e.g. `repos/repo-one/`, `repos/repo-two/`). Two rows with the same name get `repos/<name>-2/`. A leftover folder from a
  previous run is replaced by a fresh clone. The clones stay there for the **whole run** (analysis, edits, validation, commit/push/PR).
  **Clean-up:** only after *all* repositories are processed and the summary e-mail step has finished (sent, failed or skipped - also in test
  mode) the code inside every `repos/{repo-name}/` is deleted; the (now empty) `repos/{repo-name}/` folders remain. Reports are not
  inside the clones, so they survive. `output.keep_clones: true` skips the clean-up (debugging); `output.clones_dir` / `DIU_CLONES_DIR` change
  the folder. `repos/` is git-ignored; the Jenkinsfile also wipes it in `post { cleanup }` as a safety net for aborted builds.
* **Per-repository reports** - always in ONE folder, `dockerfile-reports/` in the working directory (Jenkins/Actions workspace), created if
  missing, named `{repo-name}-{DD-Mon-YYYY}.txt`: `dockerfile-reports/repo-one-22-Nov-2026.txt`, `repo-two-22-Nov-2026.txt` ...
  A report is written for **every** inventory row, including repositories that failed (the report then contains the error). Reports are
  **never** written into a cloned repository (the old `include_report_in_pr` option was removed). Two rows with the same name get
  `-2`, `-3` suffixes instead of overwriting. After writing, the tool checks that each repository has a non-empty report, logs
  `Centralized reports: N of M available in dockerfile-reports/` and warns (never fails) for a missing one.
  Change the folder with `output.dockerfile_reports_dir` or `DIU_DOCKERFILE_REPORTS_DIR`.
  One file per repository contains **all** of its Dockerfiles (root, sub-folders, `Dockerfile.dev`, `Dockerfile.stage`, ...) in the
  *Dockerfile Image Update Details* table. Example reports (mocked data) are in `docs/dockerfile-reports/`; they are documentation only -
  regenerate them with `python scripts/generate_samples.py`. The Jenkinsfile clears `dockerfile-reports/` before each build so artifacts hold only that build.
* **Other outputs** stay in `reports/`: consolidated CSV, ZIP of the repo reports, `overall-summary.txt`, `email-recipient-summary.txt`, `logs/`.

## Running in Jenkins

The repository contains a declarative `Jenkinsfile`. How it works:

1. The job runs on a Linux agent with Python (the newest of `python3.15` / `python3.14` / `python3` is used; 3.11+ required), `git` and (optional, build validation) `docker`.
2. **Validate parameters** - rejects unsafe `REPOSITORIES` / `INVENTORY_FILE` values.
3. **Prepare** - wipes `dockerfile-reports/` and `reports/` (so artifacts only hold this build), creates a venv and installs `requirements.txt`.
4. **Self-tests** - `pytest`.
5. **Run Docker image update** - binds the Jenkins credentials as environment variables and runs `python -m src.main`. Repositories are
   **dynamic**: they come from the Excel file (all rows, or only the names typed into `REPOSITORIES`). Each repository is isolated; one that fails
   or produces nothing never stops the others. The tool exits `1` when some repositories failed -> the build is marked **UNSTABLE**, not FAILED;
   any other non-zero code (crash/misconfiguration) fails the build.
6. **Collect reports** - lists `dockerfile-reports/` (the tool has already centralised the files; this step never fails).
7. **post { always }** - `archiveArtifacts` with `allowEmptyArchive: true` stores `dockerfile-reports/**/*.txt`, the CSV, ZIP, overall summary,
   recipient summary, e-mail preview and logs, so they can be downloaded from the build page ("Build Artifacts") even when the build is unstable/failed
   or no report exists.

Setup: Jenkins -> *Manage Credentials* -> add **Secret text** credentials (IDs `github-token-1`, `github-token-2`, ...) and a
**Username with password** credential `smtp-credentials`; edit the `GIT_TOKEN_CREDENTIALS` map at the top of the `Jenkinsfile` so the
variable names (`GIT_TOKEN`, `GIT_TOKEN2`, ...) match the `Token_Name` values in your Excel file; set `SMTP_SERVER`, `SMTP_PORT`, `EMAIL_FROM` as
job/global environment variables. Create a *Pipeline from SCM* job pointing at this repository. `TEST_MODE` is **on** by default (no e-mail, no PRs);
untick it to go live. To run nightly uncomment the `triggers { cron(...) }` line.

> The Jenkinsfile is covered only by static checks (structure, archiving, credentials handling). It has **not** been run on a real Jenkins server.


## Python and dependency versions

* **Python**: target the latest stable release - currently **3.15** (3.15.2, released 2026-10-25); the code requires 3.11+ and is checked in
  `.python-version`, the GitHub Actions workflow (`PYTHON_VERSION: "3.15"`) and the Jenkinsfile (newest interpreter on the agent).
* **Packages** (`requirements.txt`): `PyYAML>=6.0.4,<7`, `openpyxl>=3.1.5,<4`, `requests>=2.34.2,<3`, `pytest>=9.2,<10` (all in the one `requirements.txt`). Every install takes the newest release inside that major (verified 2026-11-22 with PyYAML 6.0.4, openpyxl 3.1.5, requests 2.34.2, pytest 9.2.0).
* **Keeping them current**: `python -m pip list --outdated`, bump the pins, run `python -m pytest -q`. `.github/dependabot.yml` does this
  weekly via pull requests (pip + GitHub Actions). Re-check on every release; a new MAJOR version is never installed automatically - bump the upper bound after testing it. For bit-for-bit reproducible builds run `pip freeze > requirements.lock` and install from that file.
