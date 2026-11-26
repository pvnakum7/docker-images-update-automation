"""Settings loading: YAML file with safe defaults, overridable by DIU_* environment variables."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "input": {"repositories_file": "config/repositories.xlsx", "selected_repositories": []},
    "upgrade": {
        "strategy": "both",             # same_major | new_major | both
        "report_only": True,            # SAFE DEFAULT: never modify Dockerfiles
        "auto_apply_major": False,      # SAFE DEFAULT: majors are only proposed
        "precision": "full",            # full -> prefer fully pinned tags; match_current -> keep current precision
        "digest_policy": "skip",        # skip | update
        "max_dockerfile_depth": 4,
        "ignore_dirs": [".git", "node_modules", "vendor", ".venv", "venv", "env", "__pycache__",
                        ".tox", "dist", "build", "target", ".idea", ".gradle", "bin", "obj", ".terraform"],
        # Number of leading version components that define a "major line" per image family.
        "release_line_depth": {"default": 1, "python": 2, "golang": 2, "php": 2, "ruby": 2, "mysql": 2},
        # Only these majors count as stable for a family (node: even majors are LTS-eligible)
        "stable_major_parity": {"node": "even"},
    },
    "registry": {"timeout_seconds": 20, "retry_count": 3, "backoff_seconds": 1.5,
                 "max_tag_pages": 30, "verify_tag_exists": True},
    "validation": {
        "buildx_check": True,           # docker buildx build --check (falls back to static check)
        "docker_build": False,          # real `docker build` (slow, needs build secrets/feeds)
        "docker_build_timeout_seconds": 1800,
        "technology": {                 # opt-in, per technology, only run when technology detected
            "enabled": False,
            "timeout_seconds": 900,
            "commands": {
                "Node.js": ["npm ci --ignore-scripts", "npm run build --if-present"],
                "React": ["npm ci --ignore-scripts", "npm run build"],
                "Angular": ["npm ci --ignore-scripts", "npx ng build --configuration production"],
                "Python": ["pip install -r requirements.txt"],
                ".NET": ["dotnet restore", "dotnet build --no-restore"],
                "Go": ["go mod download", "go build ./..."],
                "Java Maven": ["mvn -B -q validate"],
                "Java Gradle": ["./gradlew --no-daemon build -x test"],
            },
        },
    },
    "git": {"commit_changes": False, "create_pull_requests": False, "branch_prefix": "docker-image-update",
            "base_branch": None,
            # first branch that exists on the remote wins; none present -> the repository's default branch
            "branch_priority": ["dev", "development", "develop", "main"], "commit_author_name": "DevOps Automation",
            "commit_author_email": "devops-automation@users.noreply.github.com"},
    # TEST MODE: everything runs normally EXCEPT sending the e-mail and creating pull requests
    "testing": {"enabled": False},
    "email": {"enabled": True,
              "recipients_source": "excel",   # excel: TO/CC come ONLY from the Excel file | excel_plus_secrets: also add EMAIL_TO/EMAIL_CC
               "subject": "Docker Image Upgrade Automation Report - {date}",
              "use_tls": "auto",        # auto: 465->SSL, otherwise STARTTLS | starttls | ssl | none(not recommended)
              "timeout_seconds": 60},
    "output": {"reports_dir": "reports", "dockerfile_reports_dir": "dockerfile-reports", "logs_dir": "reports/logs", "keep_clones": False, "clones_dir": "repos",
               "log_level": "INFO"},
    "exit": {"nonzero_on_repo_failure": False},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _truthy(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings(path: str | Path | None = None, env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    cfg = copy.deepcopy(DEFAULTS)
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as fh:
            cfg = _merge(cfg, yaml.safe_load(fh) or {})
    # ---- environment overrides (used by the GitHub Actions workflow inputs) ----
    if env.get("DIU_STRATEGY"):
        cfg["upgrade"]["strategy"] = env["DIU_STRATEGY"].strip().lower()
    if env.get("DIU_REPORT_ONLY"):
        cfg["upgrade"]["report_only"] = _truthy(env["DIU_REPORT_ONLY"])
    if env.get("DIU_DRY_RUN") and _truthy(env["DIU_DRY_RUN"]):
        cfg["upgrade"]["report_only"] = True           # dry-run == nothing is modified
    if env.get("DIU_AUTO_MAJOR"):
        cfg["upgrade"]["auto_apply_major"] = _truthy(env["DIU_AUTO_MAJOR"])
    if env.get("DIU_CREATE_PR"):
        cfg["git"]["create_pull_requests"] = _truthy(env["DIU_CREATE_PR"])
    if env.get("DIU_COMMIT"):
        cfg["git"]["commit_changes"] = _truthy(env["DIU_COMMIT"])
    if env.get("DIU_REPOSITORIES"):
        cfg["input"]["selected_repositories"] = [s.strip() for s in env["DIU_REPOSITORIES"].split(",") if s.strip()]
    if env.get("DIU_REPOS_FILE"):
        cfg["input"]["repositories_file"] = env["DIU_REPOS_FILE"]
    if env.get("DIU_TECH_VALIDATION"):
        cfg["validation"]["technology"]["enabled"] = _truthy(env["DIU_TECH_VALIDATION"])
    if env.get("DIU_DOCKER_BUILD"):
        cfg["validation"]["docker_build"] = _truthy(env["DIU_DOCKER_BUILD"])
    if env.get("DIU_CLONES_DIR"):
        cfg["output"]["clones_dir"] = env["DIU_CLONES_DIR"]
    if env.get("DIU_DOCKERFILE_REPORTS_DIR"):
        cfg["output"]["dockerfile_reports_dir"] = env["DIU_DOCKERFILE_REPORTS_DIR"]
    if env.get("DIU_TEST_MODE"):
        cfg["testing"]["enabled"] = _truthy(env["DIU_TEST_MODE"])
    if env.get("DIU_BRANCH_PRIORITY"):
        cfg["git"]["branch_priority"] = [b.strip() for b in env["DIU_BRANCH_PRIORITY"].split(",") if b.strip()]
    if env.get("DIU_EMAIL_ENABLED"):
        cfg["email"]["enabled"] = _truthy(env["DIU_EMAIL_ENABLED"])
    return normalise(cfg)


def normalise(cfg: dict) -> dict:
    """Validate and enforce safety invariants."""
    up = cfg["upgrade"]
    if up["strategy"] not in {"same_major", "new_major", "both"}:
        raise ValueError(f"upgrade.strategy must be same_major|new_major|both, got {up['strategy']!r}")
    if up["precision"] not in {"full", "match_current"}:
        raise ValueError("upgrade.precision must be full|match_current")
    if up["digest_policy"] not in {"skip", "update"}:
        raise ValueError("upgrade.digest_policy must be skip|update")
    if cfg["email"]["recipients_source"] not in {"excel", "excel_plus_secrets"}:
        raise ValueError("email.recipients_source must be excel|excel_plus_secrets")
    g = cfg["git"]
    prio = [str(b).strip() for b in (g.get("branch_priority") or []) if str(b).strip()]
    if g.get("base_branch"):
        prio = [str(g["base_branch"]).strip()]            # an explicit base branch overrides the priority list
    for b in prio:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", b) or ".." in b:
            raise ValueError(f"invalid branch name in git.branch_priority: {b!r}")
    g["branch_priority"] = prio
    if up["report_only"]:
        g["commit_changes"] = False
        g["create_pull_requests"] = False
    if g["create_pull_requests"]:
        g["commit_changes"] = True
    return cfg
