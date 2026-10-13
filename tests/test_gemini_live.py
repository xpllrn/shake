"""Live two-agent Gemini conversation over shake pairing + delivery.

Skipped unless GEMINI_API_KEY / AGENT_*_API_KEY is present (from .env or the environment).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shake.adapters.api import APIAdapter
from shake.config import ShakeConfig
from shake.crypto import KeyPair
from shake.delivery import DeliveryManager
from shake.pairing import PairingManager
from shake.store import Store

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _agent_env(letter: str) -> tuple[str, str, str, str]:
    prefix = f"AGENT_{letter}_"
    name = os.environ.get(f"{prefix}NAME", "Ada" if letter == "A" else "Babbage")
    provider = os.environ.get(f"{prefix}PROVIDER", "gemini")
    api_key = os.environ.get(f"{prefix}API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    model = os.environ.get(f"{prefix}MODEL", "gemini-3.5-flash-lite")
    return name, provider, api_key, model


NAME_A, PROVIDER_A, KEY_A, MODEL_A = _agent_env("A")
NAME_B, PROVIDER_B, KEY_B, MODEL_B = _agent_env("B")

pytestmark = pytest.mark.skipif(
    not KEY_A or not KEY_B,
    reason="Gemini API keys not set (GEMINI_API_KEY / AGENT_A_API_KEY / AGENT_B_API_KEY)",
)


@pytest.mark.asyncio
async def test_two_gemini_agents_pair_and_converse(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    config.ensure_directories()
    store = Store(config.db_path)
    await store.open()

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    alice_id = "1111111"
    bob_id = "2222222"
    kp_a.save(config.key_file_path(alice_id))
    kp_b.save(config.key_file_path(bob_id))
    await store.create_agent(alice_id, NAME_A, kp_a.public_bytes)
    await store.create_agent(bob_id, NAME_B, kp_b.public_bytes)

    pm = PairingManager(store, config)
    res = await pm.initiate_connect(alice_id, bob_id)
    code = res["code"]
    assert await pm.verify_code(alice_id, bob_id, code) is True
    assert await pm.verify_code(bob_id, alice_id, code) is True

    adapter_a = APIAdapter(
        agent_id=alice_id,
        name=NAME_A,
        provider=PROVIDER_A,
        api_key=KEY_A,
        model=MODEL_A,
        store=store,
    )
    adapter_b = APIAdapter(
        agent_id=bob_id,
        name=NAME_B,
        provider=PROVIDER_B,
        api_key=KEY_B,
        model=MODEL_B,
        store=store,
    )
    await adapter_a.heartbeat()
    await adapter_b.heartbeat()

    dm = DeliveryManager(store, config)

    opener = (
        f"Hello {NAME_B}, this is {NAME_A}. Please say hello back, and in one "
        "short sentence say whether we can talk to each other over this channel."
    )
    sent, ack = await dm.send_text(alice_id, bob_id, opener)
    assert ack.status == "ok"

    reply_b = await dm.execute_adapter_turn(
        sent, adapter_b, {"peer_name": NAME_A}
    )
    assert reply_b is not None, f"{NAME_B} returned NOOP"
    assert reply_b.status == "ok"
    body_b = str(reply_b.body).strip()
    assert "hello" in body_b.lower()
    print(f"\n{NAME_A} -> {NAME_B}: {opener}")
    print(f"{NAME_B} -> {NAME_A}: {body_b}")

    follow_up = (
        f"{NAME_A}, {NAME_B} said hello. Please say hello back to {NAME_B} "
        "and confirm in one short sentence that you two can talk to each other."
    )
    sent2, ack2 = await dm.send_text(bob_id, alice_id, follow_up)
    assert ack2.status == "ok"

    reply_a = await dm.execute_adapter_turn(
        sent2, adapter_a, {"peer_name": NAME_B}
    )
    assert reply_a is not None, f"{NAME_A} returned NOOP"
    assert reply_a.status == "ok"
    body_a = str(reply_a.body).strip()
    assert "hello" in body_a.lower()
    print(f"{NAME_B} -> {NAME_A}: {follow_up}")
    print(f"{NAME_A} -> {NAME_B}: {body_a}")

    history = await store.get_history(alice_id, bob_id)
    assert len(history) >= 3

    await store.close()
