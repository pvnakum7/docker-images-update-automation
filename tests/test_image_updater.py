import hashlib
import json
from pathlib import Path

from src.image_updater import ImageUpdater
from src.models import RepoResult
from tests.helpers import FakeRegistry, default_registry, make_validator, settings, write

MULTI = """# build stage
FROM --platform=linux/amd64 node:22.22.0-slim AS builder
RUN npm ci && npm run build
FROM nginx:1.27-alpine AS production
COPY --from=builder /app/dist /usr/share/nginx/html
"""


def run(tmp_path, files, *, report_only=False, registry=None, strategy="both", auto_major=False, validator=None, **extra):
    s = settings(strategy=strategy, report_only=report_only, auto_apply_major=auto_major, **extra)
    for rel, content in files.items():
        write(tmp_path / rel, content)
    v, _ = validator or make_validator(s)
    upd = ImageUpdater(s, registry or default_registry(), v)
    res = RepoResult("repo", "https://github.com/o/repo")
    upd.process_repository(tmp_path, res)
    return res


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def by_idx(res, path="Dockerfile"):
    d = next(d for d in res.dockerfiles if d.path == path)
    return d, {(r.instruction_index, r.upgrade_type): r for r in d.rows}


def test_report_only_never_modifies(tmp_path):
    write(tmp_path / "Dockerfile", MULTI)
    before = sha(tmp_path / "Dockerfile")
    res = run(tmp_path, {}, report_only=True)
    assert sha(tmp_path / "Dockerfile") == before
    d, rows = by_idx(res)
    assert d.modified is False and d.from_count == 2
    assert rows[(1, "Same Major")].status == "UPDATE_AVAILABLE" and rows[(1, "Same Major")].new_image == "node:22.24.0-slim"
    assert rows[(2, "Same Major")].new_image == "nginx:1.29.0-alpine"
    assert rows[(1, "New Major")].status == "REQUIRES_VALIDATION" and rows[(1, "New Major")].new_image == "node:26.13.0-slim"
    assert res.updated_images == 0 and res.pending_images == 2 and res.major_recommendations == 1     # nginx has no newer major in the fixture


def test_same_major_update_applied_and_everything_else_preserved(tmp_path):
    res = run(tmp_path, {"Dockerfile": MULTI})
    new = (tmp_path / "Dockerfile").read_text()
    assert new == MULTI.replace("node:22.22.0-slim", "node:22.24.0-slim").replace("nginx:1.27-alpine", "nginx:1.29.0-alpine")
    d, rows = by_idx(res)
    assert d.modified and rows[(1, "Same Major")].status == "UPDATED" and rows[(2, "Same Major")].status == "UPDATED"
    assert res.updated_images == 2 and res.updated_dockerfiles == 1 and res.total_from == 2
    assert d.validation_status == "PASSED"
    # major proposals were NOT applied
    assert "26.13" not in new


def test_new_major_only_strategy_never_applies_by_default(tmp_path):
    res = run(tmp_path, {"Dockerfile": MULTI}, strategy="new_major")
    assert (tmp_path / "Dockerfile").read_text() == MULTI
    assert res.updated_images == 0 and res.major_recommendations == 1
    d, rows = by_idx(res)
    assert set(r.upgrade_type for r in d.rows) == {"New Major"}


def test_auto_major_requires_established_compatibility(tmp_path):
    files = {"Dockerfile": "FROM node:22.22.0-slim\n", "package.json": json.dumps({"engines": {"node": ">=20"}})}
    res = run(tmp_path, files, strategy="both", auto_major=True)
    d, rows = by_idx(res)
    assert rows[(1, "New Major")].status == "UPDATED"
    assert (tmp_path / "Dockerfile").read_text() == "FROM node:26.13.0-slim\n"
    assert rows[(1, "Same Major")].status == "SKIPPED" and "Superseded" in rows[(1, "Same Major")].remarks


def test_auto_major_blocked_when_project_pins_older_major(tmp_path):
    files = {"Dockerfile": "FROM node:22.22.0-slim\n", "package.json": json.dumps({"engines": {"node": "^22"}})}
    res = run(tmp_path, files, strategy="both", auto_major=True)
    d, rows = by_idx(res)
    assert rows[(1, "New Major")].status == "REQUIRES_VALIDATION"
    assert (tmp_path / "Dockerfile").read_text() == "FROM node:22.24.0-slim\n"       # same-major still applied


def test_major_recommended_status_when_constraints_allow(tmp_path):
    files = {"Dockerfile": "FROM node:22.22.0-slim\n", "package.json": json.dumps({"engines": {"node": ">=20"}})}
    res = run(tmp_path, files, report_only=True)
    _, rows = by_idx(res)
    assert rows[(1, "New Major")].status == "MAJOR_UPGRADE_RECOMMENDED"


def test_up_to_date(tmp_path):
    res = run(tmp_path, {"Dockerfile": "FROM node:22.24.0-slim\n"})
    d, rows = by_idx(res)
    assert rows[(1, "Same Major")].status == "UP_TO_DATE" and not d.modified
    assert res.up_to_date_images == 1 and res.up_to_date_dockerfiles == 1


def test_skip_cases(tmp_path):
    t = ("ARG BASE\nARG GOOD=22.22.0-slim\nFROM scratch AS a\nFROM ${BASE} AS b\nFROM busybox\nFROM node:latest\n"
         "FROM unknown/img:1.0\nFROM node:22.22.0-slim@sha256:" + "a" * 64 + " AS dg\nFROM a\nFROM Bad_Image:1\nFROM node:${GOOD} AS ok\n")
    res = run(tmp_path, {"Dockerfile": t})
    d, rows = by_idx(res)
    st = {i: next(r.status for (idx, _k), r in rows.items() if idx == i) for i in range(1, 10)}
    assert st[1] == "SKIPPED"          # scratch
    assert st[2] == "SKIPPED"          # unresolved ARG
    assert st[3] == "NOT_SUPPORTED"    # no tag
    assert st[4] == "NOT_SUPPORTED"    # latest
    assert st[5] == "SKIPPED"          # registry: not found -> skipped w/ reason
    assert st[6] == "SKIPPED"          # digest pinned
    assert st[7] == "SKIPPED"          # stage ref
    assert st[8] == "INVALID_IMAGE"
    assert "not found" in next(r.remarks for (i, _), r in rows.items() if i == 5).lower()
    assert "ARG BASE" in next(r.remarks for (i, _), r in rows.items() if i == 2)
    assert st[9] == "UPDATE_AVAILABLE" or st[9] == "UPDATED"
    assert d.from_count == 9 and set(d.outcomes) == set(range(1, 10))
    assert res.total_from == 9


def test_digest_policy_update_repins(tmp_path):
    t = "FROM node:22.22.0-slim@sha256:" + "a" * 64 + "\n"
    res = run(tmp_path, {"Dockerfile": t}, digest_policy="update")
    assert (tmp_path / "Dockerfile").read_text() == "FROM node:22.24.0-slim@sha256:" + "b" * 64 + "\n"


def test_arg_based_from_updates_default_only(tmp_path):
    t = "ARG NODE=22.22.0-slim\nFROM node:${NODE}\nRUN echo hi\n"
    res = run(tmp_path, {"Dockerfile": t})
    assert (tmp_path / "Dockerfile").read_text() == "ARG NODE=22.24.0-slim\nFROM node:${NODE}\nRUN echo hi\n"
    assert res.updated_images == 1


def test_arg_reused_is_reported_not_guessed(tmp_path):
    t = "ARG NODE=22.22.0-slim\nFROM node:${NODE}\nRUN echo $NODE\n"
    res = run(tmp_path, {"Dockerfile": t})
    assert (tmp_path / "Dockerfile").read_text() == t
    _, rows = by_idx(res)
    assert rows[(1, "Same Major")].status == "SKIPPED" and "referenced" in rows[(1, "Same Major")].remarks


def test_registry_failure_isolated_per_image(tmp_path):
    reg = default_registry()
    reg.errors[("docker.io", "library/nginx")] = "network"
    res = run(tmp_path, {"Dockerfile": MULTI}, registry=reg)
    d, rows = by_idx(res)
    assert rows[(1, "Same Major")].status == "UPDATED"
    assert rows[(2, "Same Major")].status == "SKIPPED" and "unreachable" in rows[(2, "Same Major")].remarks


def test_auth_and_rate_limit_reasons(tmp_path):
    reg = FakeRegistry({}, errors={("container-registry.oracle.com", "database/free"): "auth",
                                   ("docker.io", "library/node"): "rate_limit"})
    res = run(tmp_path, {"Dockerfile": "FROM container-registry.oracle.com/database/free:23.4.0\nFROM node:22.1.0\n"}, registry=reg)
    d, rows = by_idx(res)
    assert "authentication" in rows[(1, "Same Major")].remarks and rows[(1, "Same Major")].registry == "Oracle Container Registry"
    assert "rate limit" in rows[(2, "Same Major")].remarks


def test_tag_missing_in_registry_manifest_check(tmp_path):
    reg = default_registry()
    reg.missing_tags = {"22.24.0-slim"}
    res = run(tmp_path, {"Dockerfile": "FROM node:22.22.0-slim\n"}, registry=reg, strategy="same_major")
    d, rows = by_idx(res)
    assert rows[(1, "Same Major")].status == "FAILED" and "not found" in rows[(1, "Same Major")].remarks
    assert (tmp_path / "Dockerfile").read_text() == "FROM node:22.22.0-slim\n"
    assert res.failed_images == 1 and res.failed_dockerfiles == 1


def test_validation_failure_restores_original(tmp_path):
    s = settings(report_only=False, strategy="same_major")
    v, runner = make_validator(s, rc=1, out="ERROR: dockerfile parse error", docker=True)
    # make baseline pass, then new check fail
    calls = {"n": 0}
    orig = runner.__call__

    def flaky(cmd, **kw):
        if "--check" in cmd:
            calls["n"] += 1
            runner.rc = 0 if calls["n"] == 1 else 1
            runner.out = "" if calls["n"] == 1 else "ERROR: failed to parse"
        return orig(cmd, **kw)
    v.runner = flaky
    write(tmp_path / "Dockerfile", "FROM node:22.22.0-slim\n")
    upd = ImageUpdater(s, default_registry(), v)
    res = RepoResult("r", "l")
    upd.process_repository(tmp_path, res)
    assert (tmp_path / "Dockerfile").read_text() == "FROM node:22.22.0-slim\n"
    d, rows = by_idx(res)
    assert rows[(1, "Same Major")].status == "VALIDATION_FAILED" and not d.modified
    assert res.updated_images == 0 and res.failed_images == 1 and res.failed_dockerfiles == 1
    assert not list(tmp_path.glob(".diu-*"))                      # no temp files left behind


def test_preexisting_buildx_warnings_not_blamed(tmp_path):
    s = settings(report_only=False, strategy="same_major")
    v, runner = make_validator(s, rc=1, out="WARN: FromAsCasing: 'as' and 'FROM' keywords' casing do not match", docker=True)
    write(tmp_path / "Dockerfile", "FROM node:22.22.0-slim AS a\n")
    res = RepoResult("r", "l")
    ImageUpdater(s, default_registry(), v).process_repository(tmp_path, res)
    assert (tmp_path / "Dockerfile").read_text() == "FROM node:22.24.0-slim AS a\n"
    assert res.updated_images == 1


def test_static_check_fallback_when_docker_missing(tmp_path):
    res = run(tmp_path, {"Dockerfile": "FROM node:22.22.0-slim\n"}, strategy="same_major")
    d, _ = by_idx(res)
    assert d.validation_status == "PASSED" and "static" in d.validation_details


def test_technology_validation_not_executed_when_tool_missing(tmp_path):
    s = settings(report_only=False, strategy="same_major")
    v, _ = make_validator(s, docker=False, technology={"enabled": True, "timeout_seconds": 5, "commands": {"Node.js": ["npm ci"]}})
    files = {"Dockerfile": "FROM node:22.22.0-slim\n", "package.json": "{}"}
    for rel, c in files.items():
        write(tmp_path / rel, c)
    res = RepoResult("r", "l")
    ImageUpdater(s, default_registry(), v).process_repository(tmp_path, res)
    d, rows = by_idx(res)
    assert rows[(1, "Same Major")].status == "VALIDATION_NOT_EXECUTED"
    assert "not installed" in rows[(1, "Same Major")].remarks
    assert res.updated_images == 1       # file changed; counted as applied, but not claimed validated


def test_tech_validation_only_for_detected_technologies(tmp_path):
    s = settings(report_only=False, strategy="same_major")
    v, runner = make_validator(s, docker=True, technology={"enabled": True, "timeout_seconds": 5,
        "commands": {"Node.js": ["npm ci"], "Go": ["go build ./..."], "Python": ["pip install -r requirements.txt"]}})
    write(tmp_path / "Dockerfile", "FROM node:22.22.0-slim\n")
    write(tmp_path / "package.json", "{}")
    res = RepoResult("r", "l")
    ImageUpdater(s, default_registry(), v).process_repository(tmp_path, res)
    cmds = [" ".join(c) for c in runner.calls]
    assert any(c.startswith("npm ci") for c in cmds)
    assert not any("go build" in c or "pip install" in c for c in cmds)


def test_crlf_and_non_utf8_roundtrip(tmp_path):
    p = tmp_path / "Dockerfile"
    p.write_bytes(b"FROM node:22.22.0-slim\r\nRUN echo caf\xe9\r\n")
    run(tmp_path, {}, strategy="same_major")
    assert p.read_bytes() == b"FROM node:22.24.0-slim\r\nRUN echo caf\xe9\r\n"


def test_one_bad_dockerfile_does_not_stop_others(tmp_path):
    files = {"Dockerfile": "FROM node:22.22.0-slim\n", "bad/Dockerfile": "RUN nothing\n", "empty/Dockerfile": ""}
    res = run(tmp_path, files, strategy="same_major")
    assert res.total_dockerfiles == 3 and res.updated_dockerfiles == 1
    assert res.skipped_dockerfiles == 2 and res.total_from == 1


def test_multiple_dockerfiles_and_depth(tmp_path):
    files = {"Dockerfile": "FROM node:22.22.0-slim\n", "svc/api/Dockerfile.prod": "FROM python:3.12.5-slim\n",
             "a/b/c/d/Dockerfile": "FROM golang:1.22.1\n", "a/b/c/d/e/Dockerfile": "FROM golang:1.22.1\n"}
    res = run(tmp_path, files, strategy="same_major")
    assert res.total_dockerfiles == 3
    assert (tmp_path / "svc/api/Dockerfile.prod").read_text() == "FROM python:3.12.9-slim\n"      # python stays 3.12
    assert (tmp_path / "a/b/c/d/Dockerfile").read_text() == "FROM golang:1.22.9\n"
    assert (tmp_path / "a/b/c/d/e/Dockerfile").read_text() == "FROM golang:1.22.1\n"           # beyond depth 4


def test_dotnet_and_java_multi_stage_independent(tmp_path):
    t = ("FROM mcr.microsoft.com/dotnet/sdk:8.0.100 AS build\nFROM mcr.microsoft.com/dotnet/aspnet:8.0.1 AS final\n")
    res = run(tmp_path, {"Dockerfile": t, "App.csproj": "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>"}, strategy="both", report_only=True)
    d, rows = by_idx(res)
    assert rows[(1, "Same Major")].new_image == "mcr.microsoft.com/dotnet/sdk:8.0.404"
    assert rows[(2, "Same Major")].new_image == "mcr.microsoft.com/dotnet/aspnet:8.0.11"
    assert rows[(1, "New Major")].new_image == "mcr.microsoft.com/dotnet/sdk:9.0.200"
    assert rows[(1, "New Major")].status == "MAJOR_UPGRADE_RECOMMENDED"       # net8.0 >= 8 constraint allows 9
    assert rows[(1, "Same Major")].registry == "Microsoft MCR"


def test_daemon_unavailable_is_not_treated_as_bad_dockerfile(tmp_path):
    """Regression: real `docker buildx build --check` without a daemon prints 'failed to connect to the docker API'."""
    s = settings(report_only=False, strategy="same_major")
    msg = "ERROR: failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is correct and if the daemon is running"
    v, _ = make_validator(s, rc=1, out=msg, docker=True)
    write(tmp_path / "Dockerfile", "FROM node:22.22.0-slim\n")
    res = RepoResult("r", "l")
    ImageUpdater(s, default_registry(), v).process_repository(tmp_path, res)
    assert (tmp_path / "Dockerfile").read_text() == "FROM node:22.24.0-slim\n"
    d, _ = by_idx(res)
    assert d.validation_status == "PASSED" and "static" in d.validation_details
