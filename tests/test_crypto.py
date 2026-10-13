"""Unit tests for cryptographic primitives and ID generation."""

import re
from pathlib import Path

from shake.crypto import (
    KeyPair,
    compute_pairing_code,
    constant_time_compare,
    generate_agent_id,
    generate_nonce,
    generate_room_id,
    verify_signature,
)


def test_agent_id_format() -> None:
    for _ in range(100):
        aid = generate_agent_id()
        assert len(aid) == 7
        assert aid.isdigit()
        assert aid[0] != "0"


def test_room_id_format() -> None:
    for _ in range(100):
        rid = generate_room_id()
        assert len(rid) == 6
        assert rid.isdigit()
        assert rid[0] != "0"


def test_keypair_generation_and_signatures() -> None:
    kp = KeyPair.generate()
    assert len(kp.public_bytes) == 32
    assert len(kp.private_bytes) == 32

    msg = b"hello from shake agent"
    sig = kp.sign(msg)
    assert len(sig) == 64
    assert verify_signature(kp.public_bytes, msg, sig) is True
    # Tampered message fails
    assert verify_signature(kp.public_bytes, b"tampered message", sig) is False
    # Other public key fails
    kp2 = KeyPair.generate()
    assert verify_signature(kp2.public_bytes, msg, sig) is False


def test_keypair_save_load_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "keys" / "1234567.key"
    kp = KeyPair.generate()
    kp.save(key_path)

    assert key_path.exists()
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600

    loaded = KeyPair.load(key_path)
    assert loaded.public_bytes == kp.public_bytes
    assert loaded.private_bytes == kp.private_bytes


def test_fingerprint_format() -> None:
    kp = KeyPair.generate()
    fp = kp.fingerprint()
    # Format should be 8 groups of 4 hex digits separated by colon (16 bytes = 32 hex chars)
    # e.g., a1b2:c3d4:...
    parts = fp.split(":")
    assert len(parts) == 8
    for part in parts:
        assert len(part) == 4
        assert re.match(r"^[0-9a-f]{4}$", part)


def test_constant_time_compare() -> None:
    assert constant_time_compare("secret123", "secret123") is True
    assert constant_time_compare("secret123", "secret124") is False
    assert constant_time_compare(b"\x01\x02", b"\x01\x02") is True
    assert constant_time_compare(b"\x01\x02", b"\x01\x03") is False


def test_pairing_code_computation() -> None:
    kp_a = KeyPair.generate()
    kp_b = KeyPair.generate()
    nonce_a = generate_nonce()
    nonce_b = generate_nonce()

    # Code computed by A
    code_a = compute_pairing_code(
        pubkey_a=kp_a.public_bytes,
        nonce_a=nonce_a,
        pubkey_b=kp_b.public_bytes,
        nonce_b=nonce_b,
    )

    # Code computed by B (with order of arguments swapped)
    code_b = compute_pairing_code(
        pubkey_a=kp_b.public_bytes,
        nonce_a=nonce_b,
        pubkey_b=kp_a.public_bytes,
        nonce_b=nonce_a,
    )

    assert len(code_a) == 6
    assert code_a.isdigit()
    # Symmetry property: both sides must compute the exact same 6 digits
    assert code_a == code_b
