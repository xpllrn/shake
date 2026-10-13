"""Integration tests for shaked daemon over Unix domain socket."""

import asyncio
from pathlib import Path

import pytest

from shake.client import ShakeClient
from shake.config import ShakeConfig
from shake.daemon import Daemon


@pytest.mark.asyncio
async def test_daemon_lifecycle_and_agent_ipc(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    daemon = Daemon(config)

    # Start daemon in background task
    task = asyncio.create_task(daemon.run())
    client = ShakeClient(config)

    # Poll until ready
    for _ in range(50):
        if config.socket_path.exists():
            try:
                res = await client.call("ping")
                if res.get("data") == "pong":
                    break
            except Exception:
                pass
        await asyncio.sleep(0.05)

    # 1. Test ping
    ping_res = await client.call("ping")
    assert ping_res.get("data") == "pong"

    # 2. Test status
    status_res = await client.call("status")
    status_data = status_res.get("data", {})
    assert status_data.get("agents_count") == 0
    assert status_data.get("socket") == str(config.socket_path)

    # 3. Create agent
    create_res = await client.call("agent_create", {"name": "AgentAlpha"})
    create_data = create_res.get("data", {})
    agent_id = create_data.get("agent_id")
    assert agent_id is not None
    assert len(agent_id) == 7
    assert create_data.get("name") == "AgentAlpha"

    # Verify key file was created with 0600 mode
    key_file = config.key_file_path(agent_id)
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600

    # 4. List agents
    list_res = await client.call("agent_list")
    agents = list_res.get("data", {}).get("agents", [])
    assert len(agents) == 1
    assert agents[0]["agent_id"] == agent_id
    assert agents[0]["name"] == "AgentAlpha"

    # 5. Get agent
    get_res = await client.call("agent_get", {"agent_id": agent_id})
    assert get_res.get("data", {}).get("agent_id") == agent_id

    # 6. Stop daemon gracefully
    stop_res = await client.call("stop")
    assert stop_res.get("data", {}).get("stopped") is True

    await task
    assert not config.socket_path.exists()


@pytest.mark.asyncio
async def test_daemon_messaging_ipc(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    daemon = Daemon(config)
    task = asyncio.create_task(daemon.run())
    client = ShakeClient(config)

    for _ in range(50):
        if config.socket_path.exists():
            try:
                res = await client.call("ping")
                if res.get("data") == "pong":
                    break
            except Exception:
                pass
        await asyncio.sleep(0.05)

    # 1. Create two agents
    a1 = (await client.call("agent_create", {"name": "Alice"}))["data"]["agent_id"]
    a2 = (await client.call("agent_create", {"name": "Bob"}))["data"]["agent_id"]

    # 2. Pair them
    conn_a = (await client.call("connect", {"local_agent_id": a1, "peer_id": a2}))["data"]
    code = conn_a["code"]
    await client.call("verify", {"local_agent_id": a1, "peer_id": a2, "code": code})
    await client.call("verify", {"local_agent_id": a2, "peer_id": a1, "code": code})

    # 3. Heartbeat
    hb_res = await client.call("heartbeat", {"agent_id": a2, "status": "online"})
    assert hb_res["data"]["heartbeat"] == "recorded"

    # 4. Send text from Alice to Bob
    text_res = await client.call(
        "text",
        {
            "sender_id": a1,
            "target_id": a2,
            "body": "Hello Bob from Daemon IPC",
        },
    )
    assert text_res["data"]["status"] == "acked"
    msg_id = text_res["data"]["msg_id"]

    # 5. Check Bob's inbox
    inbox_res = await client.call("inbox", {"local_agent_id": a2})
    msgs = inbox_res["data"]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["msg_id"] == msg_id
    assert msgs[0]["body"] == "Hello Bob from Daemon IPC"

    # 6. Check history
    hist_res = await client.call("history", {"local_agent_id": a1, "peer_id": a2})
    assert len(hist_res["data"]["messages"]) == 1

    await client.call("stop")
    await task
