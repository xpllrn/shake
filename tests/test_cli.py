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


def test_cli_good_at_and_contacts_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHAKE_HOME", str(tmp_path))
    runner.invoke(app, ["init"])

    # Create agent
    res_create = runner.invoke(app, ["agent", "create", "Alice"])
    assert res_create.exit_code == 0
    alice_id = res_create.stdout.strip()

    # Set good-at offline
    res_good_at = runner.invoke(app, ["agent", "good-at", "Python, Cryptography"])
    assert res_good_at.exit_code == 0
    assert f"Updated good_at for agent {alice_id}: 'Python, Cryptography'" in res_good_at.stdout

    # Contact list offline (empty)
    res_contacts = runner.invoke(app, ["contact", "list"])
    assert res_contacts.exit_code == 0
    assert "No contacts found" in res_contacts.stdout


def test_cli_handshake_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import re
    import threading
    import time

    from shake.config import ShakeConfig
    from shake.daemon import Daemon

    monkeypatch.setenv("SHAKE_HOME", str(tmp_path))
    runner.invoke(app, ["init"])

    # Create Alice and Bob
    res_a = runner.invoke(app, ["agent", "create", "Alice"])
    alice_id = res_a.stdout.strip()
    res_b = runner.invoke(app, ["agent", "create", "Bob"])
    bob_id = res_b.stdout.strip()

    config = ShakeConfig(home=tmp_path)
    stop_event = threading.Event()

    def daemon_thread() -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        daemon = Daemon(config)

        async def _run() -> None:
            t = asyncio.create_task(daemon.run())
            while not stop_event.is_set():
                await asyncio.sleep(0.05)
            daemon.stop()
            await t

        loop.run_until_complete(_run())
        loop.close()

    t = threading.Thread(target=daemon_thread, daemon=True)
    t.start()

    # Wait for daemon socket to be ready
    for _ in range(50):
        if config.socket_path.exists():
            break
        time.sleep(0.05)
    assert config.socket_path.exists()

    try:
        # 1. Alice connects to Bob
        monkeypatch.setenv("SHAKE_AGENT", alice_id)
        res_conn1 = runner.invoke(app, ["connect", bob_id])
        assert res_conn1.exit_code == 0
        assert f"Connect request delivered to {bob_id}" in res_conn1.stdout

        # 2. Bob views contact requests
        monkeypatch.setenv("SHAKE_AGENT", bob_id)
        res_reqs = runner.invoke(app, ["contact", "requests"])
        assert res_reqs.exit_code == 0
        assert alice_id in res_reqs.stdout

        # 3. Bob accepts connection
        res_conn2 = runner.invoke(app, ["connect", alice_id])
        assert res_conn2.exit_code == 0
        assert "Pairing request accepted!" in res_conn2.stdout
        assert "Mutual verification code:" in res_conn2.stdout

        # Extract 6-digit code
        match = re.search(r"\b(\d{6})\b", res_conn2.stdout)
        assert match is not None
        code = match.group(1)

        # 4. Alice verifies with 6-digit code
        monkeypatch.setenv("SHAKE_AGENT", alice_id)
        res_verify = runner.invoke(app, ["verify", bob_id, code])
        assert res_verify.exit_code == 0
        assert "Mutual verification successful!" in res_verify.stdout

        # 5. List contacts
        res_contacts = runner.invoke(app, ["contact", "list"])
        assert res_contacts.exit_code == 0
        assert bob_id in res_contacts.stdout
        assert "verified" in res_contacts.stdout

        # 6. Trust, block, unblock
        res_trust = runner.invoke(app, ["contact", "trust", bob_id])
        assert res_trust.exit_code == 0
        assert f"Contact {bob_id} marked as trusted" in res_trust.stdout

        res_block = runner.invoke(app, ["contact", "block", bob_id])
        assert res_block.exit_code == 0
        assert f"Contact {bob_id} is now blocked" in res_block.stdout

        res_unblock = runner.invoke(app, ["contact", "unblock", bob_id])
        assert res_unblock.exit_code == 0
        assert f"Contact {bob_id} has been unblocked" in res_unblock.stdout

        # 7. Disconnect
        res_disc = runner.invoke(app, ["disconnect", bob_id])
        assert res_disc.exit_code == 0
        assert f"Disconnected from agent {bob_id}" in res_disc.stdout

    finally:
        stop_event.set()
        t.join(timeout=3.0)

