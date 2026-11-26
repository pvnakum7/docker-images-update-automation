import json

import pytest

from src.dockerfile_scanner import find_dockerfiles, is_dockerfile_name
from src.tech_detector import (ANGULAR, DOTNET, GO, JAVA, JAVA_GRADLE, JAVA_MAVEN, NODE, PYTHON, REACT,
                               assess_major_compat, detect_technologies, parse_constraint, satisfies, summarize,
                               techs_for_dockerfile)
from tests.helpers import write


def names(root):
    return set(summarize(detect_technologies(root)))


def test_node(tmp_path):
    write(tmp_path / "package.json", json.dumps({"name": "x", "dependencies": {"express": "4"}}))
    write(tmp_path / ".nvmrc", "v22.1.0\n")
    assert names(tmp_path) == {NODE}


def test_react(tmp_path):
    write(tmp_path / "package.json", json.dumps({"dependencies": {"react": "18", "react-dom": "18"}}))
    assert names(tmp_path) == {NODE, REACT}


def test_angular(tmp_path):
    write(tmp_path / "angular.json", "{}")
    write(tmp_path / "package.json", json.dumps({"dependencies": {"@angular/core": "17"}}))
    assert names(tmp_path) == {NODE, ANGULAR}


@pytest.mark.parametrize("fname", ["requirements.txt", "pyproject.toml", "Pipfile", "setup.py"])
def test_python(tmp_path, fname):
    write(tmp_path / fname, "x")
    assert names(tmp_path) == {PYTHON}


@pytest.mark.parametrize("fname", ["App.csproj", "App.sln", "global.json"])
def test_dotnet(tmp_path, fname):
    write(tmp_path / fname, "{}" if fname.endswith("json") else "<Project/>")
    assert names(tmp_path) == {DOTNET}


def test_go(tmp_path):
    write(tmp_path / "go.mod", "module x\n\ngo 1.22\n")
    assert names(tmp_path) == {GO}


def test_java_maven(tmp_path):
    write(tmp_path / "pom.xml", "<project><properties><java.version>17</java.version></properties></project>")
    assert names(tmp_path) == {JAVA_MAVEN, JAVA}
    write(tmp_path / "mvnw", "#!/bin/sh")


def test_java_gradle(tmp_path):
    write(tmp_path / "build.gradle.kts", "java { toolchain { languageVersion.set(JavaLanguageVersion.of(21)) } }")
    assert names(tmp_path) == {JAVA_GRADLE, JAVA}


def test_mixed_repo_and_no_ignored_dirs(tmp_path):
    write(tmp_path / "frontend/package.json", json.dumps({"dependencies": {"react": "18"}}))
    write(tmp_path / "backend/requirements.txt", "flask")
    write(tmp_path / "svc/pom.xml", "<project/>")
    write(tmp_path / "node_modules/leftpad/package.json", "{}")
    write(tmp_path / "go/go.mod", "module a\ngo 1.21\n")
    f = detect_technologies(tmp_path)
    assert {x.name for x in f} == {NODE, REACT, PYTHON, JAVA_MAVEN, JAVA, GO}
    assert not any("node_modules" in i for x in f for i in x.indicators)


def test_nothing_detected(tmp_path):
    write(tmp_path / "README.md", "hi")
    assert detect_technologies(tmp_path) == []


def test_dockerfile_tech_scoping(tmp_path):
    write(tmp_path / "frontend/package.json", json.dumps({"dependencies": {"react": "1"}}))
    write(tmp_path / "backend/requirements.txt", "x")
    f = detect_technologies(tmp_path)
    assert {t.name for t in techs_for_dockerfile("backend/Dockerfile", f)} == {PYTHON}
    assert {t.name for t in techs_for_dockerfile("Dockerfile", f)} == {NODE, REACT, PYTHON}


def test_constraints():
    assert satisfies(parse_constraint(">=18"), (26,))
    assert not satisfies(parse_constraint("^20"), (26,))
    assert satisfies(parse_constraint(">=3.9,<4"), (3, 14))
    assert not satisfies(parse_constraint(">=3.9,<3.13"), (3, 14))
    assert parse_constraint("18 || 20") is None


def test_major_compat(tmp_path):
    write(tmp_path / "package.json", json.dumps({"engines": {"node": ">=18"}, "dependencies": {"react": "1"}}))
    f = detect_technologies(tmp_path)
    ok, _ = assess_major_compat("library/node", (26,), f)
    assert ok
    ok, why = assess_major_compat("library/python", (3, 14), f)
    assert not ok and "no matching technology" in why
    ok, why = assess_major_compat("library/nginx", (2,), f)
    assert not ok
    ok, why = assess_major_compat("library/alpine", (4,), f)
    assert not ok and "not mapped" in why
    write(tmp_path / ".nvmrc", "22")
    f = detect_technologies(tmp_path)
    ok, why = assess_major_compat("library/node", (26,), f)   # .nvmrc pins 22 -> excludes 26
    assert not ok and "does not allow" in why


def test_dockerfile_names():
    for n in ["Dockerfile", "Dockerfile.dev", "Dockerfile.prod", "Dockerfile.test", "Dockerfile.custom",
              "Dockerfile-api", "Dockerfile_worker", "api.Dockerfile", "dockerfile"]:
        assert is_dockerfile_name(n), n
    for n in ["Dockerfile.md", "Dockerfile.dockerignore", "README.md", "Dockerfile.bak", "docker-compose.yml", "Dockerfiles"]:
        assert not is_dockerfile_name(n), n


def test_scanner_depth_and_ignores(tmp_path):
    for p in ["Dockerfile", "a/Dockerfile.dev", "a/b/c/d/Dockerfile", "a/b/c/d/e/Dockerfile",
              "node_modules/x/Dockerfile", ".git/Dockerfile", "vendor/Dockerfile", "venv/Dockerfile", "svc/Dockerfile.prod"]:
        write(tmp_path / p, "FROM scratch\n")
    got = find_dockerfiles(tmp_path, 4, [".git", "node_modules", "vendor", "venv"])
    assert got == ["Dockerfile", "a/Dockerfile.dev", "a/b/c/d/Dockerfile", "svc/Dockerfile.prod"]
    assert "a/b/c/d/Dockerfile" not in find_dockerfiles(tmp_path, 3, [".git", "node_modules", "vendor", "venv"])
