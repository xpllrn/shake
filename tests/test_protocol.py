"""Unit tests for envelope protocol, signing, verification, and IPC framing."""

import time

from shake.crypto import KeyPair
from shake.protocol import (
    DEFAULT_TIMESTAMP_TOLERANCE_MS,
    Envelope,
    IpcRequest,
    IpcResponse,
    pack_frame,
    unpack_frame_header,
)


def test_envelope_creation_defaults() -> None:
    now = int(time.time() * 1000)
    env = Envelope.create(
        type_="text",
        from_id="1111111",
        to_id="2222222",
        body="hello peer",
        ts=now,
    )

    assert env.v == 1
    assert env.type == "text"
    assert env.from_id == "1111111"
    assert env.to_id == "2222222"
    assert env.body == "hello peer"
    assert env.reply_by == now + 60_000
    assert len(env.nonce) > 0
    assert len(env.msg_id) == 26


def test_envelope_signing_and_verification() -> None:
    kp = KeyPair.generate()
    env = Envelope.create(
        type_="text",
        from_id="1111111",
        to_id="2222222",
        body="verified message",
    )
    env.sign(kp)
    assert len(env.sig) == 64
    assert env.verify(kp.public_bytes) is True

    # Tampering with body breaks verification
    env.body = "tampered content"
    assert env.verify(kp.public_bytes) is False


def test_envelope_serialization_roundtrip() -> None:
    kp = KeyPair.generate()
    env = Envelope.create(
        type_="text",
        from_id="1111111",
        to_id="2222222",
        body="test roundtrip",
    )
    env.sign(kp)

    encoded = env.encode()
    decoded = Envelope.decode(encoded)

    assert decoded.v == env.v
    assert decoded.msg_id == env.msg_id
    assert decoded.type == env.type
    assert decoded.from_id == env.from_id
    assert decoded.to_id == env.to_id
    assert decoded.body == env.body
    assert decoded.reply_by == env.reply_by
    assert decoded.sig == env.sig
    assert decoded.verify(kp.public_bytes) is True


def test_envelope_timestamp_validation() -> None:
    now = int(time.time() * 1000)
    env_valid = Envelope.create(
        type_="text",
        from_id="1111111",
        to_id="2222222",
        body="hi",
        ts=now - 10_000,
    )
    assert env_valid.validate_timestamp(current_time_ms=now) is True

    env_stale = Envelope.create(
        type_="text",
        from_id="1111111",
        to_id="2222222",
        body="hi",
        ts=now - (DEFAULT_TIMESTAMP_TOLERANCE_MS + 5_000),
    )
    assert env_stale.validate_timestamp(current_time_ms=now) is False


def test_ipc_framing() -> None:
    payload = b"msgpack_payload_here"
    framed = pack_frame(payload)
    assert len(framed) == 4 + len(payload)
    header = framed[:4]
    length = unpack_frame_header(header)
    assert length == len(payload)
    assert framed[4:] == payload


def test_ipc_request_response_encoding() -> None:
    req = IpcRequest(id="req1", action="agent_create", params={"name": "Alice"})
    req_encoded = req.encode()
    req_decoded = IpcRequest.decode(req_encoded)
    assert req_decoded.id == req.id
    assert req_decoded.action == req.action
    assert req_decoded.params == req.params

    resp = IpcResponse(id="req1", ok=True, data={"agent_id": "1234567"})
    resp_encoded = resp.encode()
    resp_decoded = IpcResponse.decode(resp_encoded)
    assert resp_decoded.id == resp.id
    assert resp_decoded.ok is True
    assert resp_decoded.data == resp.data
