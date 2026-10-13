"""Protocol envelopes, canonical serialization, signing, verification, and framing."""

import struct
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

import msgpack

from shake.crypto import KeyPair, verify_signature
from shake.ulid import generate_ulid

# Default clock skew tolerance: 5 minutes (300,000 ms)
DEFAULT_TIMESTAMP_TOLERANCE_MS: int = 5 * 60 * 1000

MessageType = Literal["text", "room", "ack", "reply", "status", "system"]
MessageStatus = Literal["ok", "timeout", "busy", "expired", "noop"]

ENVELOPE_CANONICAL_KEYS = [
    "body",
    "from",
    "msg_id",
    "nonce",
    "reply_by",
    "reply_to",
    "status",
    "to",
    "ts",
    "type",
    "v",
]


@dataclass
class Envelope:
    """Shake protocol message envelope."""

    v: int
    msg_id: str
    type: str
    from_id: str
    to_id: str
    ts: int
    reply_to: str | None
    reply_by: int | None
    nonce: bytes
    body: str | bytes
    status: str | None = None
    sig: bytes = b""

    @classmethod
    def create(
        cls,
        type_: MessageType,
        from_id: str,
        to_id: str,
        body: str | bytes,
        reply_to: str | None = None,
        reply_by: int | None = None,
        status: MessageStatus | None = None,
        nonce: bytes | None = None,
        msg_id: str | None = None,
        ts: int | None = None,
    ) -> "Envelope":
        """Factory method to construct a fresh envelope."""
        now_ms = ts if ts is not None else int(time.time() * 1000)
        envelope_nonce = nonce if nonce is not None else generate_ulid().encode("utf-8")[:16]

        # For text messages, default reply_by to ts + 60s if not specified
        if type_ == "text" and reply_by is None:
            reply_by = now_ms + 60_000

        return cls(
            v=1,
            msg_id=msg_id or generate_ulid(now_ms),
            type=type_,
            from_id=from_id,
            to_id=to_id,
            ts=now_ms,
            reply_to=reply_to,
            reply_by=reply_by,
            nonce=envelope_nonce,
            body=body,
            status=status,
            sig=b"",
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to raw dictionary representation matching the wire schema."""
        return {
            "v": self.v,
            "msg_id": self.msg_id,
            "type": self.type,
            "from": self.from_id,
            "to": self.to_id,
            "ts": self.ts,
            "reply_to": self.reply_to,
            "reply_by": self.reply_by,
            "nonce": self.nonce,
            "body": self.body,
            "status": self.status,
            "sig": self.sig,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Envelope":
        """Instantiate envelope from unpacked dictionary."""
        return cls(
            v=int(data["v"]),
            msg_id=str(data["msg_id"]),
            type=str(data["type"]),
            from_id=str(data["from"]),
            to_id=str(data["to"]),
            ts=int(data["ts"]),
            reply_to=str(data["reply_to"]) if data.get("reply_to") is not None else None,
            reply_by=int(data["reply_by"]) if data.get("reply_by") is not None else None,
            nonce=bytes(data["nonce"]),
            body=data["body"],
            status=str(data["status"]) if data.get("status") is not None else None,
            sig=bytes(data.get("sig", b"")),
        )

    def canonical_bytes(self) -> bytes:
        """Produce canonical msgpack bytes over all fields except 'sig'.

        Sorted dictionary keys guarantee identical binary encoding across platforms.
        """
        raw = self.to_dict()
        canonical_map = {k: raw[k] for k in ENVELOPE_CANONICAL_KEYS}
        # Pack with sorted keys
        return bytes(msgpack.packb(canonical_map, use_bin_type=True))

    def sign(self, keypair: KeyPair) -> None:
        """Sign envelope with sender's keypair."""
        canonical = self.canonical_bytes()
        self.sig = keypair.sign(canonical)

    def verify(self, pubkey: bytes) -> bool:
        """Verify the signature against sender's public key."""
        if not self.sig:
            return False
        canonical = self.canonical_bytes()
        return verify_signature(pubkey, canonical, self.sig)

    def validate_timestamp(
        self,
        current_time_ms: int | None = None,
        tolerance_ms: int = DEFAULT_TIMESTAMP_TOLERANCE_MS,
    ) -> bool:
        """Validate that the envelope timestamp is within the acceptable clock skew window."""
        now = current_time_ms if current_time_ms is not None else int(time.time() * 1000)
        return abs(now - self.ts) <= tolerance_ms

    def encode(self) -> bytes:
        """Serialize complete envelope (including signature) to msgpack bytes."""
        return bytes(msgpack.packb(self.to_dict(), use_bin_type=True))

    @classmethod
    def decode(cls, data: bytes) -> "Envelope":
        """Deserialize envelope from msgpack bytes."""
        unpacked = msgpack.unpackb(data, raw=False)
        return cls.from_dict(unpacked)


# ---------------------------------------------------------------------------
# Framing and IPC protocols
# ---------------------------------------------------------------------------


def pack_frame(payload: bytes) -> bytes:
    """Prepend a 4-byte big-endian length prefix to payload."""
    return struct.pack(">I", len(payload)) + payload


def unpack_frame_header(header: bytes) -> int:
    """Unpack 4-byte big-endian length prefix."""
    if len(header) != 4:
        raise ValueError("Header must be 4 bytes")
    (length,) = struct.unpack(">I", header)
    return int(length)


@dataclass
class IpcRequest:
    """IPC request from CLI/adapter to daemon."""

    id: str
    action: str
    params: dict[str, Any]

    def encode(self) -> bytes:
        return bytes(msgpack.packb(asdict(self), use_bin_type=True))

    @classmethod
    def decode(cls, data: bytes) -> "IpcRequest":
        raw = msgpack.unpackb(data, raw=False)
        return cls(id=str(raw["id"]), action=str(raw["action"]), params=raw.get("params", {}))


@dataclass
class IpcResponse:
    """IPC response from daemon to CLI/adapter."""

    id: str
    ok: bool
    data: Any = None
    error: str | None = None

    def encode(self) -> bytes:
        return bytes(msgpack.packb(asdict(self), use_bin_type=True))

    @classmethod
    def decode(cls, data: bytes) -> "IpcResponse":
        raw = msgpack.unpackb(data, raw=False)
        return cls(
            id=str(raw["id"]),
            ok=bool(raw["ok"]),
            data=raw.get("data"),
            error=str(raw["error"]) if raw.get("error") is not None else None,
        )
