# Contributing to shake

Thank you for contributing to `shake`! This document provides guidelines for contributing code, reporting issues, and developing locally.

---

## Security First

`shake` is built around a zero-trust cryptographic model:
1. **Never trust ambient network metadata**: Every message and handshake envelope must carry an Ed25519 digital signature verified against pinned public keys.
2. **Key pinning is permanent**: Once a peer's public key is verified, it is permanently pinned in the local database. It is never overwritten or implicitly replaced.
3. **Out-of-band verification**: SAS (Short Authentication String) codes must be confirmed independently by both operators/agents. Verification codes must never be auto-filled or implicitly approved.
4. **No secret leakage**: Never log private keys, session tokens, or unencrypted payloads.

Any changes touching `src/shake/crypto.py`, `src/shake/pairing.py`, or `src/shake/delivery.py` must maintain these invariants and include comprehensive unit tests.

---

## Local Development

### Prerequisites

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) (recommended) or standard `pip`

### Setup

Clone the repository and install the editable package with development dependencies:

```bash
git clone https://github.com/mvanhorn/shake.git  # or your fork
cd shake
uv venv
source .venv/bin/activate
make install
```

### Development Commands

We provide a `Makefile` with standard developer commands:

```bash
# Run the complete test suite
make test

# Check code formatting and linter issues
make lint

# Auto-format code
make fmt

# Run strict type checking
make typecheck

# Run full CI quality gate (lint + typecheck + test)
make check

# Build wheel and source distributions
make build

# Clean temporary build and cache artifacts
make clean
```

---

## Code Style & Standards

- **Formatting & Linting**: We use [Ruff](https://astral.sh/ruff) with a maximum line length of 100 characters.
- **Type Annotations**: All code in `src/` and `tests/` must be fully typed and pass `mypy --disallow-untyped-defs`.
- **Async Safety**: `shaked` and IPC components use `asyncio` with `aiosqlite`. Ensure all database access and network I/O are non-blocking.

---

## Submitting Pull Requests

1. Fork the repository and create your branch from `main`:
   ```bash
   git checkout -b feature/my-improvement
   ```
2. Write unit tests for your changes in `tests/`.
3. Verify that `make check` passes with zero errors and 100% test pass rate.
4. If your PR introduces architectural trade-offs or security decisions, document them in `DECISIONS.md`.
5. Open a pull request against `main` with a clear explanation of what changed and why.
