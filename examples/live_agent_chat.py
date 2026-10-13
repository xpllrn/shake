#!/usr/bin/env python3
"""Live Two-Agent Demonstration over Shake.

Demonstrates two AI agents (Ada and Babbage) running with Google Gemini (AI Studio),
completing mutual cryptographic pairing and key pinning, and exchanging signed 1:1 messages.

Usage:
    python examples/live_agent_chat.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from shake.adapters.api import APIAdapter
from shake.config import ShakeConfig
from shake.crypto import KeyPair
from shake.delivery import DeliveryManager
from shake.pairing import PairingManager
from shake.store import Store

console = Console()


def load_env() -> None:
    """Load credentials from .env if present."""
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


async def main() -> None:
    load_env()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("AGENT_A_API_KEY", "")
    if not api_key:
        console.print("[bold red]Error:[/bold red] GEMINI_API_KEY not found in .env.")
        sys.exit(1)

    console.print(
        Panel(
            "[bold cyan]Shake Live Agent-to-Agent Demonstration[/bold cyan]\n"
            "Backend: Google AI Studio (Gemini)\n"
            "Transport: Cryptographic Envelopes + Ed25519 Signatures + Key Pinning",
            border_style="cyan",
        )
    )

    with tempfile.TemporaryDirectory() as td:
        config = ShakeConfig(home=Path(td))
        config.ensure_directories()
        store = Store(config.db_path)
        await store.open()

        try:
            # 1. Identity Generation
            console.print("\n[bold]1. Initializing Agent Identities...[/bold]")
            kp_a = KeyPair.generate()
            kp_b = KeyPair.generate()
            ada_id = "1049281"
            babbage_id = "2094821"

            kp_a.save(config.key_file_path(ada_id))
            kp_b.save(config.key_file_path(babbage_id))

            await store.create_agent(
                ada_id, "Ada", kp_a.public_bytes, good_at="Architecture, Security"
            )
            await store.create_agent(
                babbage_id, "Babbage", kp_b.public_bytes, good_at="Algorithms, Data"
            )

            console.print(f"  [green]✓[/green] Created Agent: [bold]Ada[/bold] ({ada_id})")
            console.print(f"  [green]✓[/green] Created Agent: [bold]Babbage[/bold] ({babbage_id})")

            # 2. Mutual Cryptographic Handshake (SAS)
            console.print("\n[bold]2. Executing Mutual Pairing Handshake...[/bold]")
            pm = PairingManager(store, config)
            res = await pm.initiate_connect(ada_id, babbage_id)
            sas_code = res["code"]
            console.print(
                f"  [cyan]Mutual 6-digit SAS code:[/cyan] "
                f"[bold white on blue] {sas_code} [/bold white on blue]"
            )

            # Independent verification
            v_a = await pm.verify_code(ada_id, babbage_id, sas_code)
            v_b = await pm.verify_code(babbage_id, ada_id, sas_code)
            assert v_a and v_b
            console.print("  [green]✓[/green] Both sides confirmed SAS and pinned public keys.")

            # 3. Setup Adapters & Delivery Manager
            dm = DeliveryManager(store, config)
            model_name = os.environ.get("AGENT_A_MODEL", "gemini-3.5-flash-lite")

            adapter_ada = APIAdapter(
                agent_id=ada_id,
                name="Ada",
                provider="gemini",
                api_key=api_key,
                model=model_name,
                store=store,
            )
            adapter_babbage = APIAdapter(
                agent_id=babbage_id,
                name="Babbage",
                provider="gemini",
                api_key=api_key,
                model=model_name,
                store=store,
            )
            await adapter_ada.heartbeat()
            await adapter_babbage.heartbeat()

            # 4. Multi-Turn Conversation
            console.print("\n[bold]3. Starting Live Agent-to-Agent Dialogue...[/bold]\n")

            turns = [
                (
                    "Ada",
                    ada_id,
                    "Babbage",
                    babbage_id,
                    adapter_babbage,
                    "Hello Babbage! We have established an authenticated channel. "
                    "In two sentences, how does Ed25519 key pinning protect against MITM?",
                ),
                (
                    "Babbage",
                    babbage_id,
                    "Ada",
                    ada_id,
                    adapter_ada,
                    "Thanks Ada. How should we handle replay attacks and message deadlines?",
                ),
            ]

            for sender_name, s_id, recipient_name, r_id, recipient_adapter, prompt in turns:
                console.print(f"[bold magenta]➔ {sender_name}[/bold magenta]: {prompt}")

                sent_env, ack_env = await dm.send_text(s_id, r_id, prompt)
                ack_short = ack_env.msg_id[:8]
                console.print(f"  [dim]↳ [green]ACK received[/green] (id={ack_short}...)[/dim]")

                reply_env = await dm.execute_adapter_turn(
                    sent_env, recipient_adapter, {"peer_name": sender_name}
                )
                if reply_env:
                    reply_text = (
                        reply_env.body.decode("utf-8", errors="replace")
                        if isinstance(reply_env.body, bytes)
                        else str(reply_env.body)
                    )
                    console.print(f"[bold cyan]⬅ {recipient_name}[/bold cyan]: {reply_text}\n")

            # 5. Review Monotonic Inbox & History
            console.print("[bold]4. Inspecting Scoped Message History...[/bold]")
            history = await store.get_history(ada_id, babbage_id)
            console.print(f"  [green]✓[/green] Total verified messages stored: {len(history)}")
            for m in history:
                direction = "OUT" if m.direction == "outbound" else "IN "
                body_raw = m.body or ""
                body_text = (
                    body_raw.decode("utf-8", errors="replace")
                    if isinstance(body_raw, bytes)
                    else str(body_raw)
                )
                body_prev = body_text[:60].replace("\n", " ")
                console.print(f"  [dim]{direction} | {m.msg_id[:10]} | {body_prev}...[/dim]")

            console.print("\n[bold green]✓ Demonstration completed successfully![/bold green]\n")

        finally:
            await store.close()


if __name__ == "__main__":
    asyncio.run(main())
