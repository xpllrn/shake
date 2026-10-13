"""Unit tests for SQLite storage and migrations."""

from pathlib import Path

import pytest

from shake.crypto import KeyPair
from shake.store import Store


@pytest.mark.asyncio
async def test_store_migrations_and_integrity(tmp_path: Path) -> None:
    db_path = tmp_path / "shake.db"
    store = Store(db_path)
    await store.open()

    integrity = await store.check_integrity()
    assert integrity == "ok"

    await store.close()


@pytest.mark.asyncio
async def test_store_agent_crud(tmp_path: Path) -> None:
    db_path = tmp_path / "shake.db"
    store = Store(db_path)
    await store.open()

    kp = KeyPair.generate()
    created = await store.create_agent("1234567", "Alice", kp.public_bytes)
    assert created.agent_id == "1234567"
    assert created.name == "Alice"

    fetched = await store.get_agent("1234567")
    assert fetched is not None
    assert fetched.name == "Alice"
    assert fetched.pubkey == kp.public_bytes

    all_agents = await store.list_agents()
    assert len(all_agents) == 1
    assert all_agents[0].agent_id == "1234567"

    deleted = await store.delete_agent("1234567")
    assert deleted is True

    fetched_after = await store.get_agent("1234567")
    assert fetched_after is None

    await store.close()
