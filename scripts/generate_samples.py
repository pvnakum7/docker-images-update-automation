"""Generate sample reports for several technology stacks.

IMPORTANT: registry responses are MOCKED (tests/helpers.py) and repositories are local fixtures, so the
version numbers in the examples are illustrative - they are NOT live registry data.

    python scripts/generate_samples.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook  # noqa: E402

import src.main as main  # noqa: E402
import src.email_sender as es  # noqa: E402
from tests.helpers import default_registry, write  # noqa: E402

REPOS = {
    "react-storefront": {
        "package.json": json.dumps({"name": "shop", "engines": {"node": ">=20"}, "dependencies": {"react": "18.2.0", "react-dom": "18.2.0"}}),
        "Dockerfile": "FROM node:22.22.0-slim AS builder\nWORKDIR /app\nCOPY . .\nRUN npm ci && npm run build\n\nFROM nginx:1.27-alpine AS production\nCOPY --from=builder /app/build /usr/share/nginx/html\n"},
    "angular-portal": {
        "angular.json": "{}", "package.json": json.dumps({"dependencies": {"@angular/core": "17.3.0"}}), ".nvmrc": "22\n",
        "Dockerfile": "FROM --platform=linux/amd64 node:22.22.0-slim AS build\nRUN npm ci\nFROM nginx:1.27-alpine\n"},
    "python-etl": {
        "requirements.txt": "pandas\n", "pyproject.toml": "[project]\nrequires-python = \">=3.9\"\n",
        "Dockerfile": "FROM python:3.12.5-slim\nRUN pip install -r requirements.txt\n",
        "worker/Dockerfile.prod": "FROM python:3.11.9-slim AS base\n"},
    "dotnet-api": {
        "Api.csproj": "<Project Sdk=\"Microsoft.NET.Sdk.Web\"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>",
        "global.json": "{\"sdk\": {\"version\": \"8.0.100\"}}",
        "Dockerfile": "FROM mcr.microsoft.com/dotnet/sdk:8.0.100 AS build\nRUN dotnet publish -c Release\nFROM mcr.microsoft.com/dotnet/aspnet:8.0.1 AS final\n"},
    "go-gateway": {
        "go.mod": "module gw\n\ngo 1.22\n", "go.sum": "",
        "Dockerfile": "FROM golang:1.22.1 AS build\nRUN go build ./...\nFROM gcr.io/distroless/static:nonroot\n"},
    "java-maven-service": {
        "pom.xml": "<project><properties><java.version>17</java.version></properties></project>", "mvnw": "#!/bin/sh\n",
        "Dockerfile": "FROM maven:3.9.6-eclipse-temurin-17 AS build\nRUN mvn -B package\nFROM eclipse-temurin:17.0.9_9-jre\n"},
    "java-gradle-service": {
        "build.gradle.kts": "java { toolchain { languageVersion.set(JavaLanguageVersion.of(25)) } }",
        "Dockerfile": "ARG JRE_TAG=17.0.9_9-jre\nFROM eclipse-temurin:${JRE_TAG}\n",
        "backend/Dockerfile": "FROM eclipse-temurin:17.0.9_9-jre\n"},
    "inventory-system": {      # custom Dockerfile names in several folders
        "Dockerfile": "FROM python:3.12.5-slim\n",
        "frontend/package.json": json.dumps({"dependencies": {"react": "18"}}),
        "frontend/Dockerfile": "FROM node:22.22.0-slim AS build\nFROM nginx:1.27-alpine\n",
        "frontend/Dockerfile.dev": "FROM node:22.22.0-slim\n",
        "backend/Dockerfile.stage": "FROM python:3.12.5-slim\n"},
    "polyglot-monorepo": {
        "frontend/package.json": json.dumps({"dependencies": {"react": "18"}}), "frontend/Dockerfile": "FROM node:22.22.0-slim\n",
        "backend/requirements.txt": "flask\n", "backend/Dockerfile": "FROM python:3.12.5-slim\n",
        "svc/pom.xml": "<project/>", "svc/Dockerfile": "FROM ghcr.io/org/api:v2.0\n"},
}


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


class Capture:
    sent = []

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, context=None): pass
    def login(self, u, p): pass
    def send_message(self, msg, from_addr=None, to_addrs=None): Capture.sent.append(msg)


def main_():
    tmp = Path(tempfile.mkdtemp())
    wb = Workbook(); ws = wb.active
    ws.append(["Repo_Name", "Repo_Link", "POC", "Email", "CC_Email", "Token_Name"])
    for i, (name, files) in enumerate(REPOS.items()):
        p = tmp / "repos" / name
        p.mkdir(parents=True)
        git(p, "init", "-q", "-b", "main")
        for rel, c in files.items():
            write(p / rel, c)
        git(p, "add", "-A"); git(p, "commit", "-q", "-m", "init")
        ws.append([name, f"https://github.com/example-org/{name}", f"Person-{chr(65 + i)}", f"person.{chr(97 + i)}@example.com", "lead@example.com", "GIT_TOKEN" if i % 2 == 0 else "GIT_TOKEN2"])
    ws.append(["private-missing", "https://github.com/example-org/private-missing", "Person-Z", "z@example.com", "", "GIT_TOKEN"])
    inv = tmp / "repos.xlsx"; wb.save(inv)

    # point each GitHub URL at its local fixture so the run is offline
    from src.github_manager import GitHubManager
    real_clone = GitHubManager.clone

    def local_clone(self, link, dest, branch=None):
        name = link.rstrip("/").rsplit("/", 1)[-1]
        src = tmp / "repos" / name
        if not src.exists():
            from src.github_manager import GitAccessError
            raise GitAccessError("not_found", "repository not found (or token has no access to this private repository)")
        return real_clone(self, str(src), dest)

    GitHubManager.clone = local_clone
    GitHubManager.select_branch = lambda self, link, prio: None        # offline: no ls-remote
    main.RegistryClient = lambda **kw: default_registry()
    es.smtplib.SMTP = Capture
    out = tmp / "out"
    env = {"SMTP_SERVER": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "x",
           "EMAIL_FROM": "devops-automation@example.com",
           "GIT_TOKEN": "example-token-not-real-1", "GIT_TOKEN2": "example-token-not-real-2"}
    settings = tmp / "settings.yaml"
    settings.write_text("upgrade:\n  strategy: both\n  report_only: true\noutput:\n  logs_dir: %s/logs\n  dockerfile_reports_dir: %s/dockerfile-reports\n  clones_dir: %s/repos\n" % (out, out, out))
    main.run(["--config", str(settings), "--repos-file", str(inv), "--output-dir", str(out)], env=env)

    # Only the per-repository example reports are kept (project root: dockerfile-reports/)
    dst = ROOT / "dockerfile-reports"
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    for f in (out / "dockerfile-reports").glob("*.txt"):
        shutil.copy(f, dst / f.name)
    shutil.rmtree(tmp, ignore_errors=True)
    print("example reports written to", dst)


if __name__ == "__main__":
    main_()
