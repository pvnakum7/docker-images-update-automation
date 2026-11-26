"""Static validation of the GitHub Actions workflow (syntax + the requirements it must satisfy)."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github/workflows/docker-image-update.yml"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WF.read_text())


@pytest.fixture(scope="module")
def raw():
    return WF.read_text()


def triggers(wf):
    return wf.get("on", wf.get(True))      # PyYAML parses the bare key `on` as boolean True


def test_valid_yaml_and_top_level(wf):
    assert wf["name"] and set(wf["jobs"]) == {"gate", "update"}


def test_manual_and_scheduled_triggers(wf):
    t = triggers(wf)
    assert "workflow_dispatch" in t and "schedule" in t
    cron = t["schedule"][0]["cron"]
    assert len(cron.split()) == 5
    inputs = t["workflow_dispatch"]["inputs"]
    for name in ["repositories", "upgrade_strategy", "dry_run", "run_mode", "auto_apply_major"]:
        assert name in inputs
    assert set(inputs["upgrade_strategy"]["options"]) == {"same_major", "new_major", "both"}
    assert set(inputs["run_mode"]["options"]) == {"report_only", "update_and_create_pr"}
    assert inputs["dry_run"]["default"] is True and inputs["auto_apply_major"]["default"] is False


def test_least_privilege_and_concurrency(wf):
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"]["cancel-in-progress"] is False and wf["concurrency"]["group"]


def test_steps_present_in_order(wf):
    steps = wf["jobs"]["update"]["steps"]
    names = [s.get("name", "") for s in steps]
    order = ["Check out", "Set up Python", "Install system packages", "Set up Docker Buildx", "Install Python dependencies",
             "Verify toolchain", "Run self-tests", "Resolve run parameters", "Run Docker image update", "Upload reports"]
    idx = [next(i for i, n in enumerate(names) if n.startswith(o)) for o in order]
    assert idx == sorted(idx)
    assert wf["jobs"]["update"]["needs"] == "gate" and "gate.outputs.run" in wf["jobs"]["update"]["if"]


def test_action_versions_are_current_majors_and_pinned(raw):
    uses = re.findall(r"uses:\s*([\w./-]+)@(\S+)", raw)
    got = dict(uses)
    assert got == {"actions/checkout": "v7", "actions/setup-python": "v7", "docker/setup-buildx-action": "v4", "actions/upload-artifact": "v7"}


def test_secrets_only_in_env_never_in_run_bodies(wf, raw):
    for job in wf["jobs"].values():
        for s in job["steps"]:
            assert "secrets." not in s.get("run", ""), s.get("name")
    for sec in ["GH_PAT", "SMTP_SERVER", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO", "EMAIL_CC"]:
        assert f"secrets.{sec}" in raw
    for n in ["GIT_TOKEN"] + [f"GIT_TOKEN{i}" for i in range(2, 11)]:
        assert f"secrets.{n}" in raw
    assert "DIU_TEST_MODE" in raw and "test_mode" in raw
    run_step = next(s for s in wf["jobs"]["update"]["steps"] if s.get("name") == "Run Docker image update")
    assert "GITHUB_TOKEN" in run_step["env"] and "GH_PAT" in run_step["env"]["GITHUB_TOKEN"]
    assert "echo $SMTP" not in raw and "set -x" not in raw


def test_untrusted_inputs_only_reach_env_not_shell_expansion(wf):
    for s in wf["jobs"]["update"]["steps"]:
        assert "inputs." not in s.get("run", ""), "inputs must be passed via env"


def test_artifact_upload_always_runs_and_installs_pinned_deps(wf):
    up = next(s for s in wf["jobs"]["update"]["steps"] if s.get("uses", "").startswith("actions/upload-artifact"))
    assert up["if"] == "always()" and up["with"]["path"].split() == ["dockerfile-reports/", "reports/"]
    inst = next(s for s in wf["jobs"]["update"]["steps"] if s.get("name") == "Install Python dependencies")
    assert "requirements.txt" in inst["run"] and "pip check" in inst["run"] and "set -euo pipefail" in inst["run"]
    sp = next(s for s in wf["jobs"]["update"]["steps"] if s.get("uses", "").startswith("actions/setup-python"))
    assert sp["with"]["cache"] == "pip"


def test_requirements_are_bounded_to_a_major_version():
    """Latest release within the current major: never unpinned (a breaking major must not arrive silently)."""
    import re
    for f in ["requirements.txt"]:
        for line in (ROOT / f).read_text().splitlines():
            if line and not line.startswith(("#", "-r")):
                assert re.match(r"^[A-Za-z0-9_.-]+>=[\d.]+,<\d+$", line), line


def test_gate_and_param_scripts_are_valid_bash(wf, tmp_path):
    import subprocess
    for job in wf["jobs"].values():
        for s in job["steps"]:
            if "run" in s:
                f = tmp_path / "s.sh"
                f.write_text(s["run"])
                assert subprocess.run(["bash", "-n", str(f)]).returncode == 0, s.get("name")
