"""Cryptographic primitives for identity, key management, signatures, and pairing codes."""

import hashlib
import hmac
import os
import secrets
from pathlib import Path

import nacl.encoding
import nacl.exceptions
import nacl.signing
import nacl.utils


def generate_agent_id() -> str:
    """Generate a 7-digit numeric agent ID that never starts with 0 (1000000 - 9999999)."""
    return str(secrets.randbelow(9000000) + 1000000)


def generate_room_id() -> str:
    """Generate a 6-digit numeric room ID that never starts with 0 (100000 - 999999)."""
    return str(secrets.randbelow(900000) + 100000)


def generate_nonce(length: int = 32) -> bytes:
    """Generate cryptographically secure random bytes for envelope nonces and pairing."""
    return secrets.token_bytes(length)


class KeyPair:
    """Ed25519 signing keypair wrapper."""

    def __init__(self, signing_key: nacl.signing.SigningKey) -> None:
        self.signing_key = signing_key
        self.verify_key = signing_key.verify_key

    @classmethod
    def generate(cls) -> "KeyPair":
        """Generate a new random Ed25519 keypair."""
        sk = nacl.signing.SigningKey.generate()
        return cls(sk)

    @classmethod
    def from_seed(cls, seed: bytes) -> "KeyPair":
        """Reconstruct keypair from 32-byte raw private seed."""
        sk = nacl.signing.SigningKey(seed)
        return cls(sk)

    @classmethod
    def load(cls, path: Path) -> "KeyPair":
        """Load private key from file (verifying 0600 mode)."""
        if not path.is_file():
            raise FileNotFoundError(f"Key file not found: {path}")
        # Verify permissions on non-Windows platforms
        mode = path.stat().st_mode & 0o777
        if mode & 0o077 != 0:
            # Enforce 0600
            path.chmod(0o600)
        seed = path.read_bytes()
        return cls.from_seed(seed)

    def save(self, path: Path) -> None:
        """Save private key seed to file with strict 0600 permissions."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        # Write seed to temporary file or direct with O_CREAT | O_WRONLY
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(bytes(self.signing_key))
        except Exception:
            # ensure fd closed if write failed
            pass
        path.chmod(0o600)

    @property
    def public_bytes(self) -> bytes:
        """Return 32-byte raw Ed25519 public key."""
        return bytes(self.verify_key)

    @property
    def private_bytes(self) -> bytes:
        """Return 32-byte raw Ed25519 private seed."""
        return bytes(self.signing_key)

    def sign(self, message: bytes) -> bytes:
        """Sign a message and return the 64-byte Ed25519 signature."""
        signed = self.signing_key.sign(message)
        return signed.signature

    def fingerprint(self) -> str:
        """Return human-readable short fingerprint of public key."""
        return format_fingerprint(self.public_bytes)


def verify_signature(pubkey_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 signature given the 32-byte public key."""
    if len(pubkey_bytes) != 32 or len(signature) != 64:
        return False
    try:
        vk = nacl.signing.VerifyKey(pubkey_bytes)
        vk.verify(message, signature)
        return True
    except (nacl.exceptions.BadSignatureError, Exception):
        return False


def format_fingerprint(pubkey_bytes: bytes) -> str:
    """Format 32-byte public key into grouped hex fingerprint (SHA-256 derived).

    Example format: e4a1:90f2:31bc:4588:1209:ab43:ef78:3290
    """
    digest = hashlib.sha256(pubkey_bytes).digest()
    # Take first 16 bytes (128 bits) grouped in 2-byte blocks
    groups = [digest[i : i + 2].hex() for i in range(0, 16, 2)]
    return ":".join(groups)


def constant_time_compare(val_a: str | bytes, val_b: str | bytes) -> bool:
    """Compare two strings or byte sequences in constant time."""
    if isinstance(val_a, str):
        val_a = val_a.encode("utf-8")
    if isinstance(val_b, str):
        val_b = val_b.encode("utf-8")
    return hmac.compare_digest(val_a, val_b)


def compute_pairing_code(
    pubkey_a: bytes,
    nonce_a: bytes,
    pubkey_b: bytes,
    nonce_b: bytes,
) -> str:
    """Compute the 6-digit mutual verification code locally.

    Rule: first 6 digits of decimal string SHA-256(sorted(pubkeyA, pubkeyB) || nonceA || nonceB).
    To ensure identical output regardless of which peer initiated the request,
    the (pubkey, nonce) pairs are canonically ordered by pubkey bytes.
    """
    if pubkey_a <= pubkey_b:
        data = pubkey_a + pubkey_b + nonce_a + nonce_b
    else:
        data = pubkey_b + pubkey_a + nonce_b + nonce_a

    digest = hashlib.sha256(data).digest()
    # Convert digest to integer and format as decimal string
    decimal_str = str(int.from_bytes(digest, byteorder="big"))
    # Return first 6 digits, ensuring 6 digits minimum with zero padding
    if len(decimal_str) < 6:
        decimal_str = decimal_str.zfill(6)
    return decimal_str[:6]
