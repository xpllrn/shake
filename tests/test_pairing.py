"""Unit and integration tests for mutual handshake, verification, key pinning, and contacts."""

import time
from pathlib import Path

import pytest

from shake.config import ShakeConfig
from shake.crypto import KeyPair
from shake.pairing import (
    BlockedContactError,
    InvalidCodeError,
    MaxAttemptsExceededError,
    PairingManager,
    RateLimitExceededError,
    SessionExpiredError,
)
from shake.protocol import Envelope
from shake.store import KeyPinningViolationError, Store


@pytest.mark.asyncio
async def test_pairing_connect_and_independent_verify(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    config.ensure_directories()
    store = Store(config.db_path)
    await store.open()
    pm = PairingManager(store, config)

    # Create two distinct agents
    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    kp_a.save(config.key_file_path("1111111"))
    kp_b.save(config.key_file_path("2222222"))
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    # 1. Connect initiates and resolves both sides to pending_verify
    res = await pm.initiate_connect(local_agent_id="1111111", peer_id="2222222")
    assert res["state"] == "pending_verify"
    code = res["code"]
    assert len(code) == 6
    assert code.isdigit()

    # Verify both agents now have contact state pending_verify
    c_a = await store.get_contact("1111111", "2222222")
    c_b = await store.get_contact("2222222", "1111111")
    assert c_a is not None and c_a.state == "pending_verify"
    assert c_b is not None and c_b.state == "pending_verify"

    # Crucial: Neither side has pinned keys yet
    assert c_a.pinned_at is None
    assert c_b.pinned_at is None

    # 2. Side A verifies independently
    res_a = await pm.verify_code(local_agent_id="1111111", peer_id="2222222", code=code)
    assert res_a is True

    # Check that A is verified and pinned, but B is STILL pending_verify
    c_a_verified = await store.get_contact("1111111", "2222222")
    assert c_a_verified is not None
    assert c_a_verified.state == "verified"
    assert c_a_verified.pinned_at is not None
    assert c_a_verified.pubkey == kp_b.public_bytes

    c_b_pending = await store.get_contact("2222222", "1111111")
    assert c_b_pending is not None
    assert c_b_pending.state == "pending_verify"
    assert c_b_pending.pinned_at is None

    # 3. Side B verifies independently
    res_b = await pm.verify_code(local_agent_id="2222222", peer_id="1111111", code=code)
    assert res_b is True

    c_b_verified = await store.get_contact("2222222", "1111111")
    assert c_b_verified is not None
    assert c_b_verified.state == "verified"
    assert c_b_verified.pinned_at is not None
    assert c_b_verified.pubkey == kp_a.public_bytes

    await store.close()


@pytest.mark.asyncio
async def test_pairing_via_signed_blobs(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    config.ensure_directories()
    store = Store(config.db_path)
    await store.open()
    pm = PairingManager(store, config)

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    kp_a.save(config.key_file_path("1111111"))
    kp_b.save(config.key_file_path("2222222"))
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    # 1. Agent A generates signed connect_req token
    req_env, req_token = await pm.create_connect_request("1111111", "2222222")
    assert isinstance(req_env, Envelope)
    assert req_env.type == "connect_req"
    assert isinstance(req_token, str)

    # 2. Agent B ingests token (--from-blob) and returns ack token + SAS code
    res_b = await pm.process_connect_token("2222222", req_token)
    assert res_b["state"] == "pending_verify"
    assert res_b["role"] == "receiver"
    code_b = res_b["code"]
    ack_token = res_b["token"]

    # 3. Agent A ingests ack token and receives matching SAS code
    res_a = await pm.process_connect_token("1111111", ack_token)
    assert res_a["state"] == "pending_verify"
    assert res_a["role"] == "initiator"
    code_a = res_a["code"]

    # Codes match symmetrically across air-gap/blobs
    assert code_a == code_b

    # Both verify independently
    assert await pm.verify_code("1111111", "2222222", code_a) is True
    assert await pm.verify_code("2222222", "1111111", code_b) is True

    await store.close()


@pytest.mark.asyncio
async def test_pairing_wrong_code_and_max_attempts(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    config.ensure_directories()
    store = Store(config.db_path)
    await store.open()
    pm = PairingManager(store, config)

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    kp_a.save(config.key_file_path("1111111"))
    kp_b.save(config.key_file_path("2222222"))
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    res = await pm.initiate_connect("1111111", "2222222")
    correct_code = res["code"]
    wrong_code = "000000" if correct_code != "000000" else "999999"

    # Attempt 1 fails
    with pytest.raises(InvalidCodeError) as exc_1:
        await pm.verify_code("1111111", "2222222", wrong_code)
    assert "Attempt 1 of 3" in str(exc_1.value)

    # Attempt 2 fails
    with pytest.raises(InvalidCodeError) as exc_2:
        await pm.verify_code("1111111", "2222222", wrong_code)
    assert "Attempt 2 of 3" in str(exc_2.value)

    # Attempt 3 terminates and purges session
    with pytest.raises(MaxAttemptsExceededError) as exc_3:
        await pm.verify_code("1111111", "2222222", wrong_code)
    assert "Maximum verification attempts" in str(exc_3.value)

    # Session is deleted and contact reset
    session = await store.get_pairing_session_by_peer("1111111", "2222222")
    assert session is None
    contact = await store.get_contact("1111111", "2222222")
    assert contact is not None
    assert contact.state == "none"

    await store.close()


@pytest.mark.asyncio
async def test_pairing_ttl_expiry(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    config.ensure_directories()
    store = Store(config.db_path)
    await store.open()
    pm = PairingManager(store, config)

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    kp_a.save(config.key_file_path("1111111"))
    kp_b.save(config.key_file_path("2222222"))
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    res = await pm.initiate_connect("1111111", "2222222")
    code = res["code"]

    session = await store.get_pairing_session_by_peer("1111111", "2222222")
    assert session is not None

    # Simulate expired session
    past_ms = int(time.time() * 1000) - 1000
    await store.db.execute(
        "UPDATE pairing_sessions SET expires_at = ? WHERE session_id = ?;",
        (past_ms, session.session_id),
    )
    await store.db.commit()

    with pytest.raises(SessionExpiredError):
        await pm.verify_code("1111111", "2222222", code)

    await store.close()


@pytest.mark.asyncio
async def test_pairing_key_pinning_violation_and_no_upsert(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    config.ensure_directories()
    store = Store(config.db_path)
    await store.open()
    pm = PairingManager(store, config)

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    kp_impostor = KeyPair.generate()
    kp_a.save(config.key_file_path("1111111"))
    kp_b.save(config.key_file_path("2222222"))
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    # 1. Properly pair and pin keys
    res = await pm.initiate_connect("1111111", "2222222")
    code = res["code"]
    await pm.verify_code("1111111", "2222222", code)
    await pm.verify_code("2222222", "1111111", code)

    # Verify key is pinned for Agent B
    c_b = await store.get_contact("2222222", "1111111")
    assert c_b is not None
    assert c_b.pinned_at is not None
    assert c_b.pubkey == kp_a.public_bytes

    # 2. Impostor creates connect request pretending to be 1111111 with different key
    impostor_config = ShakeConfig(home=tmp_path / "impostor")
    impostor_config.ensure_directories()
    kp_impostor.save(impostor_config.key_file_path("1111111"))
    impostor_store = Store(impostor_config.db_path)
    await impostor_store.open()
    await impostor_store.create_agent("1111111", "FakeA", kp_impostor.public_bytes)
    impostor_pm = PairingManager(impostor_store, impostor_config)

    _fake_env, fake_token = await impostor_pm.create_connect_request("1111111", "2222222")

    # Agent B must reject incoming request with KeyPinningViolationError
    with pytest.raises(KeyPinningViolationError):
        await pm.process_connect_token("2222222", fake_token)

    # 3. Direct upsert attempt on store must also raise KeyPinningViolationError
    with pytest.raises(KeyPinningViolationError):
        await store.create_or_update_contact(
            owner_agent_id="2222222",
            peer_id="1111111",
            name="Hacked",
            pubkey=kp_impostor.public_bytes,
            state="verified",
        )

    # Verify pinned key was never overwritten
    c_b_after = await store.get_contact("2222222", "1111111")
    assert c_b_after is not None
    assert c_b_after.pubkey == kp_a.public_bytes

    await impostor_store.close()
    await store.close()


@pytest.mark.asyncio
async def test_pairing_blocked_contact(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    config.ensure_directories()
    store = Store(config.db_path)
    await store.open()
    pm = PairingManager(store, config)

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    kp_a.save(config.key_file_path("1111111"))
    kp_b.save(config.key_file_path("2222222"))
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    # B blocks A
    await pm.block("2222222", "1111111")
    c_b = await store.get_contact("2222222", "1111111")
    assert c_b is not None and c_b.state == "blocked"

    # A tries to connect to B -> B rejects
    req_env, req_token = await pm.create_connect_request("1111111", "2222222")
    with pytest.raises(BlockedContactError):
        await pm.process_connect_token("2222222", req_token)

    # B unblocks A
    await pm.unblock("2222222", "1111111")
    c_b_unblocked = await store.get_contact("2222222", "1111111")
    assert c_b_unblocked is not None and c_b_unblocked.state == "none"

    await store.close()


@pytest.mark.asyncio
async def test_pairing_rate_limits(tmp_path: Path) -> None:
    config = ShakeConfig(home=tmp_path)
    config.ensure_directories()
    store = Store(config.db_path)
    await store.open()
    pm = PairingManager(store, config)

    kp_a = KeyPair.generate()
    kp_a.save(config.key_file_path("1111111"))
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)

    # Outbound rate limit: 10 per hour
    for i in range(10):
        target_id = f"200000{i}"
        await pm.create_connect_request("1111111", target_id)

    # 11th request must fail
    with pytest.raises(RateLimitExceededError) as exc_out:
        await pm.create_connect_request("1111111", "2000099")
    assert "max 10 connects/hour" in str(exc_out.value)

    # Inbound rate limit: 50 pending inbound
    kp_b = KeyPair.generate()
    kp_b.save(config.key_file_path("2222222"))
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    now_ms = int(time.time() * 1000)
    for i in range(50):
        await store.db.execute(
            """
            INSERT INTO pairing_sessions (
                session_id, local_agent_id, peer_id, nonce_local,
                state, attempts, expires_at, created_at
            ) VALUES (?, ?, ?, ?, 'requested_in', 0, ?, ?);
            """,
            (f"sess_{i}", "2222222", f"peer_{i}", b"x" * 32, now_ms + 100000, now_ms),
        )
    await store.db.commit()

    # Create AgentC with fresh outbound limit to trigger AgentB's inbound limit
    kp_c = KeyPair.generate()
    kp_c.save(config.key_file_path("3333333"))
    await store.create_agent("3333333", "AgentC", kp_c.public_bytes)

    _env, req_token = await pm.create_connect_request("3333333", "2222222")
    with pytest.raises(RateLimitExceededError) as exc_in:
        await pm.process_connect_token("2222222", req_token)
    assert "Inbound queue full: max 50" in str(exc_in.value)

    await store.close()
