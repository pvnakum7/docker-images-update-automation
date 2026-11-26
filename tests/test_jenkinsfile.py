"""Static checks of the Jenkinsfile (it cannot be executed here - no Jenkins in the test environment)."""
import re
from pathlib import Path

J = (Path(__file__).resolve().parents[1] / "Jenkinsfile").read_text()


def test_declarative_structure_and_balanced_braces():
    assert J.count("{") == J.count("}") and J.count("(") == J.count(")")
    for token in ["pipeline {", "agent any", "parameters {", "environment {", "stages {", "post {", "always {"]:
        assert token in J
    for stage in ["Validate parameters", "Prepare", "Self-tests", "Run Docker image update", "Collect reports"]:
        assert f"stage('{stage}')" in J


def test_reports_are_archived_even_when_empty():
    arch = re.search(r"archiveArtifacts[^\n]*\n?[^\n]*", J).group(0)
    assert "dockerfile-reports/**/*.txt" in arch and "allowEmptyArchive: true" in arch
    assert "post {" in J and J.index("always {") > J.index("post {")


def test_failures_in_repositories_do_not_fail_the_build():
    assert "returnStatus: true" in J and "unstable(" in J


def test_secrets_come_from_credentials_not_text():
    assert "withCredentials" in J and "string(credentialsId" in J and "usernamePassword(" in J
    assert not re.search(r"(ghp_|github_pat_)[A-Za-z0-9]", J)
    assert "DIU_DOCKERFILE_REPORTS_DIR = 'dockerfile-reports'" in J and "DIU_TEST_MODE" in J
