"""SQLite storage layer with WAL mode, automated migrations, and data access."""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

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
]


@dataclass
class AgentRecord:
    """Agent representation in local registry."""

    agent_id: str
    name: str
    pubkey: bytes
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
    async def create_agent(self, agent_id: str, name: str, pubkey: bytes) -> AgentRecord:
        now_ms = int(time.time() * 1000)
        await self.db.execute(
            "INSERT INTO agents (agent_id, name, pubkey, created_at) VALUES (?, ?, ?, ?);",
            (agent_id, name, pubkey, now_ms),
        )
        await self.db.commit()
        return AgentRecord(agent_id=agent_id, name=name, pubkey=pubkey, created_at=now_ms)

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        async with self.db.execute(
            "SELECT agent_id, name, pubkey, created_at FROM agents WHERE agent_id = ?;",
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
            )

    async def list_agents(self) -> list[AgentRecord]:
        async with self.db.execute(
            "SELECT agent_id, name, pubkey, created_at FROM agents ORDER BY created_at ASC;"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                AgentRecord(
                    agent_id=str(row["agent_id"]),
                    name=str(row["name"]),
                    pubkey=bytes(row["pubkey"]),
                    created_at=int(row["created_at"]),
                )
                for row in rows
            ]

    async def delete_agent(self, agent_id: str) -> bool:
        async with self.db.execute(
            "DELETE FROM agents WHERE agent_id = ?;",
            (agent_id,),
        ) as cursor:
            await self.db.commit()
            return cursor.rowcount > 0
