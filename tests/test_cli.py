"""CLI functional and integration tests using Typer CliRunner."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from shake.cli import app

runner = CliRunner()


def test_cli_init_and_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHAKE_HOME", str(tmp_path))

    # Test init
    res_init = runner.invoke(app, ["init"])
    assert res_init.exit_code == 0
    assert "Initialized shake home" in res_init.stdout

    # Test doctor
    res_doctor = runner.invoke(app, ["doctor"])
    assert res_doctor.exit_code == 0
    assert "All doctor checks passed" in res_doctor.stdout


def test_cli_agent_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHAKE_HOME", str(tmp_path))

    # Initialize home
    runner.invoke(app, ["init"])

    # Create agent
    res_create = runner.invoke(app, ["agent", "create", "AgentSmith"])
    assert res_create.exit_code == 0
    agent_id = res_create.stdout.strip()
    assert len(agent_id) == 7
    assert agent_id.isdigit()

    # List agents
    res_list = runner.invoke(app, ["agent", "list"])
    assert res_list.exit_code == 0
    assert agent_id in res_list.stdout
    assert "AgentSmith" in res_list.stdout

    # List agents JSON
    res_list_json = runner.invoke(app, ["agent", "list", "--json"])
    assert res_list_json.exit_code == 0
    assert f'"agent_id": "{agent_id}"' in res_list_json.stdout

    # Whoami (should resolve the only agent created)
    res_whoami = runner.invoke(app, ["agent", "whoami"])
    assert res_whoami.exit_code == 0
    assert agent_id in res_whoami.stdout

    # Fingerprint
    res_fp = runner.invoke(app, ["agent", "fingerprint", agent_id])
    assert res_fp.exit_code == 0
    assert ":" in res_fp.stdout

    # Remove agent
    res_rm = runner.invoke(app, ["agent", "rm", agent_id])
    assert res_rm.exit_code == 0
    assert f"Removed agent {agent_id}" in res_rm.stdout

    # List agents should now be empty
    res_list_empty = runner.invoke(app, ["agent", "list"])
    assert res_list_empty.exit_code == 0
    assert "No agents found" in res_list_empty.stdout
