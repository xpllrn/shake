"""Unit and integration tests for mutual handshake, verification, key pinning, and contacts."""

import time
from pathlib import Path

import pytest

from shake.crypto import KeyPair
from shake.pairing import (
    BlockedContactError,
    InvalidCodeError,
    KeyPinningViolationError,
    MaxAttemptsExceededError,
    PairingError,
    PairingManager,
    RateLimitExceededError,
    SessionExpiredError,
)
from shake.store import Store


@pytest.mark.asyncio
async def test_pairing_happy_path(tmp_path: Path) -> None:
    db_path = tmp_path / "shake.db"
    store = Store(db_path)
    await store.open()
    pm = PairingManager(store)

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    # 1. Agent A initiates connect to Agent B
    res_a = await pm.initiate_connect(
        local_agent_id="1111111",
        peer_id="2222222",
    )
    assert res_a["state"] == "requested_out"
    assert res_a["peer_id"] == "2222222"

    # Agent B should now have requested_in
    contact_b = await store.get_contact("1111111")
    assert contact_b is not None
    assert contact_b.state == "requested_in"

    # 2. Agent B accepts connect from Agent A
    res_b = await pm.initiate_connect(
        local_agent_id="2222222",
        peer_id="1111111",
    )
    assert res_b["state"] == "pending_verify"
    code = res_b["code"]
    assert len(code) == 6
    assert code.isdigit()

    # 3. Agent A verifies code
    verify_a = await pm.verify_code(local_agent_id="1111111", peer_id="2222222", code=code)
    assert verify_a["state"] == "verified"
    assert verify_a["pinned_at"] is not None

    # Agent A's contact record for B is verified and pinned
    c_a = await store.get_contact("2222222")
    assert c_a is not None
    assert c_a.state == "verified"
    assert c_a.pubkey == kp_b.public_bytes
    assert c_a.pinned_at is not None

    # 4. Agent B verifies code
    verify_b = await pm.verify_code(local_agent_id="2222222", peer_id="1111111", code=code)
    assert verify_b["state"] == "verified"
    c_b = await store.get_contact("1111111")
    assert c_b is not None
    assert c_b.state == "verified"
    assert c_b.pubkey == kp_a.public_bytes

    await store.close()


@pytest.mark.asyncio
async def test_pairing_wrong_code_and_max_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "shake.db"
    store = Store(db_path)
    await store.open()
    pm = PairingManager(store)

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    await pm.initiate_connect("1111111", "2222222")
    res_b = await pm.initiate_connect("2222222", "1111111")
    correct_code = res_b["code"]
    wrong_code = "000000" if correct_code != "000000" else "999999"

    # Attempt 1 fails
    with pytest.raises(InvalidCodeError) as exc_1:
        await pm.verify_code("1111111", "2222222", wrong_code)
    assert "2 attempts remaining" in str(exc_1.value)

    # Attempt 2 fails
    with pytest.raises(InvalidCodeError) as exc_2:
        await pm.verify_code("1111111", "2222222", wrong_code)
    assert "1 attempt remaining" in str(exc_2.value)

    # Attempt 3 fails and kills the session
    with pytest.raises(MaxAttemptsExceededError):
        await pm.verify_code("1111111", "2222222", wrong_code)

    # Session is terminated
    sess = await store.get_pairing_session_by_peer("1111111", "2222222")
    assert sess is None

    await store.close()


@pytest.mark.asyncio
async def test_pairing_expired_session(tmp_path: Path) -> None:
    db_path = tmp_path / "shake.db"
    store = Store(db_path)
    await store.open()
    pm = PairingManager(store)

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    await pm.initiate_connect("1111111", "2222222")
    res_b = await pm.initiate_connect("2222222", "1111111")
    code = res_b["code"]

    # Artificially expire the session in DB
    sess = await store.get_pairing_session_by_peer("1111111", "2222222")
    assert sess is not None
    await store.db.execute(
        "UPDATE pairing_sessions SET expires_at = ? WHERE session_id = ?;",
        (int(time.time() * 1000) - 10_000, sess.session_id),
    )
    await store.db.commit()

    with pytest.raises(SessionExpiredError):
        await pm.verify_code("1111111", "2222222", code)

    await store.close()


@pytest.mark.asyncio
async def test_key_pinning_violation(tmp_path: Path) -> None:
    db_path = tmp_path / "shake.db"
    store = Store(db_path)
    await store.open()
    pm = PairingManager(store)

    kp_a = KeyPair.generate()
    kp_b_original = KeyPair.generate()
    kp_b_impostor = KeyPair.generate()

    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)

    # Agent B was previously verified and pinned
    now_ms = int(time.time() * 1000)
    await store.create_or_update_contact(
        peer_id="2222222",
        name="AgentB",
        pubkey=kp_b_original.public_bytes,
        state="verified",
        pinned_at=now_ms,
    )

    # Attempting to connect with an impostor presenting a differing key for peer 2222222
    with pytest.raises(KeyPinningViolationError) as exc_info:
        await pm.initiate_connect(
            local_agent_id="1111111",
            peer_id="2222222",
            peer_pubkey=kp_b_impostor.public_bytes,
        )
    assert "Key pinning violation" in str(exc_info.value)

    await store.close()


@pytest.mark.asyncio
async def test_contact_blocking_and_trust(tmp_path: Path) -> None:
    db_path = tmp_path / "shake.db"
    store = Store(db_path)
    await store.open()
    pm = PairingManager(store)

    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)
    await store.create_agent("2222222", "AgentB", kp_b.public_bytes)

    # Block Agent B
    await pm.block("2222222")
    c = await store.get_contact("2222222")
    assert c is not None
    assert c.state == "blocked"

    # Cannot initiate connect to blocked contact
    with pytest.raises(BlockedContactError):
        await pm.initiate_connect("1111111", "2222222")

    # Unblock
    await pm.unblock("2222222")
    c_unblocked = await store.get_contact("2222222")
    assert c_unblocked is not None
    assert c_unblocked.state == "none"

    # Pairing and trust
    await pm.initiate_connect("1111111", "2222222")
    res_b = await pm.initiate_connect("2222222", "1111111")
    await pm.verify_code("1111111", "2222222", res_b["code"])

    # Cannot trust an unverified contact
    with pytest.raises(PairingError):
        await pm.trust("9999999")  # non-existent / not verified

    # Promote verified to trusted
    trusted = await pm.trust("2222222")
    assert trusted is True
    c_trusted = await store.get_contact("2222222")
    assert c_trusted is not None
    assert c_trusted.state == "trusted"

    # Disconnect
    disconnected = await pm.disconnect("1111111", "2222222")
    assert disconnected is True
    c_deleted = await store.get_contact("2222222")
    assert c_deleted is None

    await store.close()


@pytest.mark.asyncio
async def test_rate_limits(tmp_path: Path) -> None:
    db_path = tmp_path / "shake.db"
    store = Store(db_path)
    await store.open()
    pm = PairingManager(store)

    kp_a = KeyPair.generate()
    await store.create_agent("1111111", "AgentA", kp_a.public_bytes)

    # Create 30 fake sessions in DB to hit rate limit
    now_ms = int(time.time() * 1000)
    for i in range(30):
        await store.create_pairing_session(
            session_id=f"sess_{i}",
            local_agent_id="1111111",
            peer_id=f"peer_{i}",
            nonce_local=b"nonce",
            expires_at=now_ms + 100_000,
        )

    # 31st request should be rejected by rate limit
    with pytest.raises(RateLimitExceededError):
        await pm.initiate_connect("1111111", "3333333")

    await store.close()
