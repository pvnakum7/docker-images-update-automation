"""Shared test doubles (no network, no Docker daemon, no credentials needed)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from src.config import load_settings
from src.registry_manager import RegistryError, TagListing
from src.validator import Validator

NODE_TAGS = ["22.22.0-slim", "22.23.0-slim", "22.23.1-slim", "22-slim", "22.23-slim", "22.23.1-alpine",
             "23.5.0-slim", "24.4.0-slim", "26.13.0-slim", "26.14.0-rc.1-slim", "26.14.0-alpha-slim",
             "27.0.0-slim", "22.24.0-slim", "latest", "lts-slim", "26.13.0-alpine"]
NGINX_TAGS = ["1.27-alpine", "1.27.3-alpine", "1.28.0-alpine", "1.28-alpine", "1.29.0-alpine", "1.29.0-alpine3.20",
              "1.30.0-rc1-alpine", "stable-alpine", "latest", "mainline-alpine"]
PYTHON_TAGS = ["3.12.5-slim", "3.12.9-slim", "3.13.1-slim", "3.14.0-slim", "3.14.0rc1-slim", "3.11.9-slim"]
GOLANG_TAGS = ["1.22.1", "1.22.9", "1.23.4", "1.24.0", "1.24.1-bookworm"]
DOTNET_SDK = ["8.0.100", "8.0.404", "9.0.101", "9.0.200", "10.0.100-preview.1"]
ASPNET = ["8.0.1", "8.0.11", "9.0.0"]
MAVEN = ["3.9.6-eclipse-temurin-17", "3.9.9-eclipse-temurin-17", "3.9.9-eclipse-temurin-21", "4.0.0-eclipse-temurin-21"]
TEMURIN = ["17.0.9_9-jre", "17.0.13_11-jre", "21.0.5_11-jre", "25.0.1_8-jre"]
GHCR_API = ["v2.0", "v2.1", "v2.2", "v3.0"]


class FakeRegistry:
    """Mimics RegistryClient for the updater. Keys are (registry, repository)."""

    def __init__(self, tags: dict | None = None, errors: dict | None = None, missing_tags: set | None = None):
        self.tags = tags or {}
        self.errors = errors or {}
        self.missing_tags = missing_tags or set()
        self.calls: list[tuple] = []

    def list_tags(self, ref, name_filter=None):
        self.calls.append(("list", ref.registry, ref.repository, name_filter))
        key = (ref.registry, ref.repository)
        if key in self.errors:
            raise RegistryError(self.errors[key], f"simulated {self.errors[key]}")
        if key not in self.tags:
            raise RegistryError("not_found", "repository not found")
        return TagListing(list(self.tags[key]), True)

    def tag_exists(self, ref, tag):
        return tag not in self.missing_tags

    def get_digest(self, ref, tag):
        return "sha256:" + "b" * 64


def default_registry() -> FakeRegistry:
    return FakeRegistry({
        ("docker.io", "library/node"): NODE_TAGS,
        ("docker.io", "library/nginx"): NGINX_TAGS,
        ("docker.io", "library/python"): PYTHON_TAGS,
        ("docker.io", "library/golang"): GOLANG_TAGS,
        ("mcr.microsoft.com", "dotnet/sdk"): DOTNET_SDK,
        ("mcr.microsoft.com", "dotnet/aspnet"): ASPNET,
        ("docker.io", "library/maven"): MAVEN,
        ("docker.io", "library/eclipse-temurin"): TEMURIN,
        ("ghcr.io", "org/api"): GHCR_API,
    })


class FakeRunner:
    """Replaces subprocess.run for the validator."""

    def __init__(self, rc=0, out="", err="", docker=True, buildx_rc=None):
        self.rc, self.out, self.err, self.docker, self.buildx_rc = rc, out, err, docker, buildx_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[:3] == ["docker", "buildx", "version"]:
            return subprocess.CompletedProcess(cmd, 0 if self.docker else 1, "buildx v0", "")
        return subprocess.CompletedProcess(cmd, self.rc, self.out, self.err)


def make_validator(settings, rc=0, out="", docker=False, **cfg_over):
    for k, v in cfg_over.items():
        settings["validation"][k] = v
    runner = FakeRunner(rc=rc, out=out, docker=docker)
    return Validator(settings, runner=runner, which=lambda exe: "/usr/bin/" + exe if docker else None), runner


def settings(**upgrade):
    s = load_settings(None, {})
    s["upgrade"].update(upgrade)
    if not s["upgrade"]["report_only"]:
        pass
    return s


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    return path
