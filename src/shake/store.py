import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- Initial schema for shake
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            pubkey BLOB NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS contacts (
            peer_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            pubkey BLOB NOT NULL,
            state TEXT NOT NULL,
            pinned_at INTEGER,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pairing_sessions (
            session_id TEXT PRIMARY KEY,
            local_agent_id TEXT NOT NULL,
            peer_id TEXT NOT NULL,
            nonce_local BLOB NOT NULL,
            nonce_peer BLOB,
            code TEXT,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rooms (
            room_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS room_members (
            room_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL,
            pubkey BLOB NOT NULL,
            joined_at INTEGER NOT NULL,
            PRIMARY KEY (room_id, agent_id)
        );

        CREATE TABLE IF NOT EXISTS room_roster (
            room_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 1,
            signature BLOB NOT NULL,
            roster_payload BLOB NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            msg_id TEXT PRIMARY KEY,
            envelope BLOB NOT NULL,
            direction TEXT NOT NULL,
            state TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            reply_to TEXT,
            reply_by INTEGER,
            body TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_messages_to_ts ON messages(target_id, ts);
        CREATE INDEX IF NOT EXISTS idx_messages_from_ts ON messages(sender_id, ts);

        CREATE TABLE IF NOT EXISTS cursors (
            agent_id TEXT NOT NULL,
            stream_key TEXT NOT NULL,
            last_msg_id TEXT NOT NULL,
            last_ts INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (agent_id, stream_key)
        );

        CREATE TABLE IF NOT EXISTS claims (
            slot TEXT NOT NULL,
            room_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            lease_expires INTEGER NOT NULL,
            PRIMARY KEY (slot, room_id)
        );

        CREATE TABLE IF NOT EXISTS seen_nonces (
            nonce BLOB PRIMARY KEY,
            sender_id TEXT NOT NULL,
            seen_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_seen_nonces_expires ON seen_nonces(expires_at);

        CREATE TABLE IF NOT EXISTS heartbeats (
            agent_id TEXT PRIMARY KEY,
            last_seen INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'online',
            metadata TEXT
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE agents ADD COLUMN good_at TEXT DEFAULT '';
        ALTER TABLE contacts ADD COLUMN good_at TEXT DEFAULT '';
        """,
    ),
]


@dataclass
class AgentRecord:
    """Agent representation in local registry."""

    agent_id: str
    name: str
    pubkey: bytes
    created_at: int
    good_at: str = ""


@dataclass
class ContactRecord:
    """Peer contact record."""

    peer_id: str
    name: str
    pubkey: bytes
    state: str
    pinned_at: int | None
    updated_at: int
    good_at: str = ""


@dataclass
class PairingSessionRecord:
    """Active pairing session record."""

    session_id: str
    local_agent_id: str
    peer_id: str
    nonce_local: bytes
    nonce_peer: bytes | None
    code: str | None
    state: str
    attempts: int
    expires_at: int
    created_at: int


def run_sync_migrations(db_path: Path) -> None:
    """Synchronously apply migrations to the database file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.chmod(0o700)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA synchronous = NORMAL;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at INTEGER NOT NULL
            );
            """
        )

        cur = conn.execute("SELECT version FROM schema_migrations")
        applied_versions = {row[0] for row in cur.fetchall()}

        for version, script in MIGRATIONS:
            if version not in applied_versions:
                conn.executescript(script)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                    (version, int(time.time() * 1000)),
                )
        conn.commit()
    finally:
        conn.close()

    # Enforce 0600 on DB and journal files
    try:
        db_path.chmod(0o600)
    except OSError:
        pass


class Store:
    """Asynchronous store using aiosqlite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Open database and ensure pragmas and migrations."""
        run_sync_migrations(self.db_path)
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.execute("PRAGMA journal_mode = WAL;")
        await self._db.execute("PRAGMA foreign_keys = ON;")
        await self._db.execute("PRAGMA synchronous = NORMAL;")
        self._db.row_factory = aiosqlite.Row

    async def close(self) -> None:
        """Close open database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store database connection is not open")
        return self._db

    async def check_integrity(self) -> str:
        """Run PRAGMA integrity_check."""
        async with self.db.execute("PRAGMA integrity_check;") as cursor:
            row = await cursor.fetchone()
            return str(row[0]) if row else "unknown"

    # Agent operations
    async def create_agent(
        self, agent_id: str, name: str, pubkey: bytes, good_at: str = ""
    ) -> AgentRecord:
        now_ms = int(time.time() * 1000)
        await self.db.execute(
            "INSERT INTO agents (agent_id, name, pubkey, created_at, good_at) "
            "VALUES (?, ?, ?, ?, ?);",
            (agent_id, name, pubkey, now_ms, good_at),
        )
        await self.db.commit()
        return AgentRecord(
            agent_id=agent_id,
            name=name,
            pubkey=pubkey,
            created_at=now_ms,
            good_at=good_at,
        )

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        async with self.db.execute(
            "SELECT agent_id, name, pubkey, created_at, good_at FROM agents WHERE agent_id = ?;",
            (agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return AgentRecord(
                agent_id=str(row["agent_id"]),
                name=str(row["name"]),
                pubkey=bytes(row["pubkey"]),
                created_at=int(row["created_at"]),
                good_at=str(row["good_at"] or ""),
            )

    async def list_agents(self) -> list[AgentRecord]:
        async with self.db.execute(
            "SELECT agent_id, name, pubkey, created_at, good_at FROM agents "
            "ORDER BY created_at ASC;"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                AgentRecord(
                    agent_id=str(row["agent_id"]),
                    name=str(row["name"]),
                    pubkey=bytes(row["pubkey"]),
                    created_at=int(row["created_at"]),
                    good_at=str(row["good_at"] or ""),
                )
                for row in rows
            ]

    async def update_agent_good_at(self, agent_id: str, good_at: str) -> bool:
        async with self.db.execute(
            "UPDATE agents SET good_at = ? WHERE agent_id = ?;",
            (good_at, agent_id),
        ) as cursor:
            await self.db.commit()
            return cursor.rowcount > 0

    async def delete_agent(self, agent_id: str) -> bool:
        async with self.db.execute(
            "DELETE FROM agents WHERE agent_id = ?;",
            (agent_id,),
        ) as cursor:
            await self.db.commit()
            return cursor.rowcount > 0

    # Contact operations
    async def create_or_update_contact(
        self,
        peer_id: str,
        name: str,
        pubkey: bytes,
        state: str,
        pinned_at: int | None = None,
        good_at: str = "",
    ) -> ContactRecord:
        now_ms = int(time.time() * 1000)
        await self.db.execute(
            """
            INSERT INTO contacts (peer_id, name, pubkey, state, pinned_at, updated_at, good_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                name = excluded.name,
                pubkey = excluded.pubkey,
                state = excluded.state,
                pinned_at = COALESCE(excluded.pinned_at, contacts.pinned_at),
                updated_at = excluded.updated_at,
                good_at = CASE
                    WHEN excluded.good_at != '' THEN excluded.good_at
                    ELSE contacts.good_at
                END;
            """,
            (peer_id, name, pubkey, state, pinned_at, now_ms, good_at),
        )
        await self.db.commit()
        return ContactRecord(
            peer_id=peer_id,
            name=name,
            pubkey=pubkey,
            state=state,
            pinned_at=pinned_at,
            updated_at=now_ms,
            good_at=good_at,
        )

    async def get_contact(self, peer_id: str) -> ContactRecord | None:
        async with self.db.execute(
            "SELECT peer_id, name, pubkey, state, pinned_at, updated_at, good_at "
            "FROM contacts WHERE peer_id = ?;",
            (peer_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return ContactRecord(
                peer_id=str(row["peer_id"]),
                name=str(row["name"]),
                pubkey=bytes(row["pubkey"]),
                state=str(row["state"]),
                pinned_at=int(row["pinned_at"]) if row["pinned_at"] is not None else None,
                updated_at=int(row["updated_at"]),
                good_at=str(row["good_at"] or ""),
            )

    async def list_contacts(self, state: str | None = None) -> list[ContactRecord]:
        query = (
            "SELECT peer_id, name, pubkey, state, pinned_at, updated_at, good_at FROM contacts"
        )
        params: tuple[Any, ...] = ()
        if state:
            query += " WHERE state = ?"
            params = (state,)
        query += " ORDER BY updated_at DESC;"

        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [
                ContactRecord(
                    peer_id=str(row["peer_id"]),
                    name=str(row["name"]),
                    pubkey=bytes(row["pubkey"]),
                    state=str(row["state"]),
                    pinned_at=int(row["pinned_at"]) if row["pinned_at"] is not None else None,
                    updated_at=int(row["updated_at"]),
                    good_at=str(row["good_at"] or ""),
                )
                for row in rows
            ]

    async def update_contact_state(
        self, peer_id: str, state: str, pinned_at: int | None = None
    ) -> bool:
        now_ms = int(time.time() * 1000)
        query = "UPDATE contacts SET state = ?, updated_at = ?"
        params: list[Any] = [state, now_ms]
        if pinned_at is not None:
            query += ", pinned_at = ?"
            params.append(pinned_at)
        query += " WHERE peer_id = ?;"
        params.append(peer_id)

        async with self.db.execute(query, tuple(params)) as cursor:
            await self.db.commit()
            return cursor.rowcount > 0

    async def update_contact_good_at(self, peer_id: str, good_at: str) -> bool:
        now_ms = int(time.time() * 1000)
        async with self.db.execute(
            "UPDATE contacts SET good_at = ?, updated_at = ? WHERE peer_id = ?;",
            (good_at, now_ms, peer_id),
        ) as cursor:
            await self.db.commit()
            return cursor.rowcount > 0

    async def delete_contact(self, peer_id: str) -> bool:
        async with self.db.execute(
            "DELETE FROM contacts WHERE peer_id = ?;",
            (peer_id,),
        ) as cursor:
            await self.db.commit()
            return cursor.rowcount > 0

    # Pairing session operations
    async def create_pairing_session(
        self,
        session_id: str,
        local_agent_id: str,
        peer_id: str,
        nonce_local: bytes,
        expires_at: int,
        nonce_peer: bytes | None = None,
        code: str | None = None,
        state: str = "requested_out",
        attempts: int = 0,
    ) -> PairingSessionRecord:
        now_ms = int(time.time() * 1000)
        await self.db.execute(
            """
            INSERT INTO pairing_sessions (
                session_id, local_agent_id, peer_id, nonce_local, nonce_peer,
                code, state, attempts, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                session_id,
                local_agent_id,
                peer_id,
                nonce_local,
                nonce_peer,
                code,
                state,
                attempts,
                expires_at,
                now_ms,
            ),
        )
        await self.db.commit()
        return PairingSessionRecord(
            session_id=session_id,
            local_agent_id=local_agent_id,
            peer_id=peer_id,
            nonce_local=nonce_local,
            nonce_peer=nonce_peer,
            code=code,
            state=state,
            attempts=attempts,
            expires_at=expires_at,
            created_at=now_ms,
        )

    async def get_pairing_session(self, session_id: str) -> PairingSessionRecord | None:
        async with self.db.execute(
            """
            SELECT session_id, local_agent_id, peer_id, nonce_local, nonce_peer,
                   code, state, attempts, expires_at, created_at
            FROM pairing_sessions WHERE session_id = ?;
            """,
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return PairingSessionRecord(
                session_id=str(row["session_id"]),
                local_agent_id=str(row["local_agent_id"]),
                peer_id=str(row["peer_id"]),
                nonce_local=bytes(row["nonce_local"]),
                nonce_peer=bytes(row["nonce_peer"]) if row["nonce_peer"] is not None else None,
                code=str(row["code"]) if row["code"] is not None else None,
                state=str(row["state"]),
                attempts=int(row["attempts"]),
                expires_at=int(row["expires_at"]),
                created_at=int(row["created_at"]),
            )

    async def get_pairing_session_by_peer(
        self, local_agent_id: str, peer_id: str
    ) -> PairingSessionRecord | None:
        async with self.db.execute(
            """
            SELECT session_id, local_agent_id, peer_id, nonce_local, nonce_peer,
                   code, state, attempts, expires_at, created_at
            FROM pairing_sessions
            WHERE local_agent_id = ? AND peer_id = ?
            ORDER BY created_at DESC LIMIT 1;
            """,
            (local_agent_id, peer_id),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return PairingSessionRecord(
                session_id=str(row["session_id"]),
                local_agent_id=str(row["local_agent_id"]),
                peer_id=str(row["peer_id"]),
                nonce_local=bytes(row["nonce_local"]),
                nonce_peer=bytes(row["nonce_peer"]) if row["nonce_peer"] is not None else None,
                code=str(row["code"]) if row["code"] is not None else None,
                state=str(row["state"]),
                attempts=int(row["attempts"]),
                expires_at=int(row["expires_at"]),
                created_at=int(row["created_at"]),
            )

    async def update_pairing_session(
        self,
        session_id: str,
        state: str | None = None,
        nonce_peer: bytes | None = None,
        code: str | None = None,
        attempts: int | None = None,
    ) -> bool:
        updates: list[str] = []
        params: list[Any] = []
        if state is not None:
            updates.append("state = ?")
            params.append(state)
        if nonce_peer is not None:
            updates.append("nonce_peer = ?")
            params.append(nonce_peer)
        if code is not None:
            updates.append("code = ?")
            params.append(code)
        if attempts is not None:
            updates.append("attempts = ?")
            params.append(attempts)

        if not updates:
            return False

        query = f"UPDATE pairing_sessions SET {', '.join(updates)} WHERE session_id = ?;"
        params.append(session_id)

        async with self.db.execute(query, tuple(params)) as cursor:
            await self.db.commit()
            return cursor.rowcount > 0

    async def delete_pairing_session(self, session_id: str) -> bool:
        async with self.db.execute(
            "DELETE FROM pairing_sessions WHERE session_id = ?;",
            (session_id,),
        ) as cursor:
            await self.db.commit()
            return cursor.rowcount > 0

    async def cleanup_expired_sessions(self) -> int:
        now_ms = int(time.time() * 1000)
        async with self.db.execute(
            "DELETE FROM pairing_sessions WHERE expires_at < ?;",
            (now_ms,),
        ) as cursor:
            await self.db.commit()
            return cursor.rowcount

    async def count_recent_connect_requests(
        self, local_agent_id: str, window_ms: int = 3600_000
    ) -> int:
        cutoff = int(time.time() * 1000) - window_ms
        async with self.db.execute(
            "SELECT COUNT(*) FROM pairing_sessions WHERE local_agent_id = ? AND created_at > ?;",
            (local_agent_id, cutoff),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def count_pending_inbound_requests(self, local_agent_id: str) -> int:
        query = (
            "SELECT COUNT(*) FROM pairing_sessions "
            "WHERE local_agent_id = ? AND state = 'requested_in';"
        )
        async with self.db.execute(query, (local_agent_id,)) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
