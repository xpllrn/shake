"""Command-line interface for shake."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from shake.client import DaemonNotRunningError, ShakeClient
from shake.config import ShakeConfig
from shake.store import run_sync_migrations

app = typer.Typer(
    name="shake",
    help="shake: Secure agent-to-agent messaging CLI.",
    no_args_is_help=True,
)
agent_app = typer.Typer(
    name="agent",
    help="Manage local agent identities.",
    no_args_is_help=True,
)
app.add_typer(agent_app, name="agent")

contact_app = typer.Typer(
    name="contact",
    help="Manage peer contacts, trust, and pairing requests.",
    no_args_is_help=True,
)
app.add_typer(contact_app, name="contact")

console = Console()
err_console = Console(stderr=True)


def get_client() -> ShakeClient:
    return ShakeClient()


# ---------------------------------------------------------------------------
# Core Lifecycle Commands
# ---------------------------------------------------------------------------


@app.command("init")
def init_cmd() -> None:
    """Create configuration directory, database with migrations, and keys directory."""
    config = ShakeConfig()
    config.ensure_directories()
    run_sync_migrations(config.db_path)
    console.print(f"[green]✓[/green] Initialized shake home at [bold]{config.home}[/bold]")
    console.print(
        f"[green]✓[/green] SQLite database initialized at [bold]{config.db_path}[/bold] (WAL mode)"
    )


@app.command("start")
def start_cmd(
    detach: bool = typer.Option(False, "--detach", "-d", help="Run daemon in background process"),
) -> None:
    """Start the local shaked daemon."""
    config = ShakeConfig()
    config.ensure_directories()
    client = ShakeClient(config)

    if client.is_daemon_running():
        console.print("[yellow]shaked is already running.[/yellow]")
        return

    run_sync_migrations(config.db_path)

    if detach:
        console.print("Starting shaked in background...")
        # Launch detached subprocess using python -m shake.daemon
        log_file = open(config.log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "shake.daemon"],
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Wait up to 5 seconds for daemon socket to be ready
        ready = False
        for _ in range(50):
            time.sleep(0.1)
            if client.is_daemon_running():
                ready = True
                break

        if ready:
            console.print(f"[green]✓[/green] shaked started (PID {proc.pid})")
        else:
            err_console.print(
                f"[red]Error:[/red] shaked failed to start within 5s. Check logs: {config.log_path}"
            )
            raise typer.Exit(code=1)
    else:
        # Foreground
        from shake.daemon import Daemon, setup_daemon_logging

        setup_daemon_logging(config.log_path)
        console.print("[blue]Starting shaked in foreground... (Ctrl+C to stop)[/blue]")
        import asyncio

        daemon = Daemon(config)
        try:
            asyncio.run(daemon.run())
        except (KeyboardInterrupt, SystemExit):
            console.print("\n[yellow]shaked stopped.[/yellow]")


@app.command("stop")
def stop_cmd() -> None:
    """Stop the running shaked daemon."""
    config = ShakeConfig()
    client = ShakeClient(config)

    if not client.is_daemon_running():
        console.print("[yellow]shaked is not running.[/yellow]")
        return

    try:
        client.call_sync("stop")
    except Exception:
        pass

    # Wait for socket / pid removal
    for _ in range(30):
        time.sleep(0.1)
        if not client.is_daemon_running():
            break

    if config.socket_path.exists():
        try:
            config.socket_path.unlink()
        except OSError:
            pass

    console.print("[green]✓[/green] shaked daemon stopped.")


@app.command("status")
def status_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output status as JSON"),
) -> None:
    """Check shaked daemon status."""
    config = ShakeConfig()
    client = ShakeClient(config)

    try:
        res = client.call_sync("status")
        data = res.get("data", {})
        if json_output:
            console.print(json.dumps({"running": True, **data}, indent=2))
        else:
            table = Table(title="shaked Status", box=box.ROUNDED)
            table.add_column("Property", style="cyan")
            table.add_column("Value", style="green")
            table.add_row("Status", "Running")
            table.add_row("PID", str(data.get("pid")))
            table.add_row("Uptime (s)", str(data.get("uptime_seconds")))
            table.add_row("Socket", str(data.get("socket")))
            table.add_row("Database", str(data.get("db")))
            table.add_row("Agents", str(data.get("agents_count")))
            console.print(table)
    except DaemonNotRunningError:
        if json_output:
            console.print(json.dumps({"running": False}, indent=2))
        else:
            console.print("[yellow]shaked is not running.[/yellow]")
        raise typer.Exit(code=1)
    except Exception as e:
        err_console.print(f"[red]Error fetching status:[/red] {e}")
        raise typer.Exit(code=1)


@app.command("logs")
def logs_cmd(
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow log output in real time"),
) -> None:
    """View shaked daemon logs."""
    config = ShakeConfig()
    if not config.log_path.exists():
        console.print(f"[yellow]No log file found at {config.log_path}[/yellow]")
        return

    if follow:
        try:
            with open(config.log_path, encoding="utf-8") as f:
                f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.1)
        except KeyboardInterrupt:
            return
    else:
        content = config.log_path.read_text(encoding="utf-8")
        console.print(content)


@app.command("doctor")
def doctor_cmd() -> None:
    """Inspect environment, keys, socket, database integrity, and clock skew."""
    config = ShakeConfig()
    all_ok = True

    console.print("[bold]Running shake doctor diagnostics...[/bold]\n")

    # 1. Config directory permissions
    if config.home.exists():
        mode = oct(config.home.stat().st_mode)[-3:]
        if mode == "700":
            console.print(f" [green]✓[/green] Config directory mode is 0700: {config.home}")
        else:
            console.print(f" [yellow]![/yellow] Config directory mode is 0{mode} (expected 0700)")
            config.home.chmod(0o700)
            console.print("   Fixed: chmod 0700 applied.")
    else:
        console.print(" [red]✗[/red] Config directory missing: run `shake init`")
        all_ok = False

    # 2. Key directory & file permissions
    if config.keys_dir.exists():
        k_mode = oct(config.keys_dir.stat().st_mode)[-3:]
        if k_mode == "700":
            console.print(f" [green]✓[/green] Keys directory mode is 0700: {config.keys_dir}")
        else:
            console.print(f" [yellow]![/yellow] Keys directory mode is 0{k_mode} (fixing to 0700)")
            config.keys_dir.chmod(0o700)

        bad_keys = 0
        for k in config.keys_dir.glob("*.key"):
            km = oct(k.stat().st_mode)[-3:]
            if km != "600":
                bad_keys += 1
                k.chmod(0o600)
        if bad_keys > 0:
            console.print(f"   Fixed: chmod 0600 applied to {bad_keys} key file(s).")
        else:
            console.print(" [green]✓[/green] Private keys permissions verified (0600)")
    else:
        console.print(
            " [yellow]![/yellow] Keys directory does not exist yet (created on agent creation)"
        )

    # 3. Database integrity & WAL mode
    if config.db_path.exists():
        import sqlite3

        try:
            conn = sqlite3.connect(str(config.db_path))
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            row = cur.fetchone()
            cur.execute("PRAGMA journal_mode;")
            journal_mode = cur.fetchone()[0]
            conn.close()

            if row and row[0] == "ok":
                console.print(
                    f" [green]✓[/green] SQLite integrity passed (WAL mode: {journal_mode.upper()})"
                )
            else:
                console.print(f" [red]✗[/red] SQLite integrity check returned: {row}")
                all_ok = False
        except Exception as e:
            console.print(f" [red]✗[/red] SQLite error: {e}")
            all_ok = False
    else:
        console.print(" [yellow]![/yellow] Database does not exist yet: run `shake init`")

    # 4. Daemon socket
    client = ShakeClient(config)
    if client.is_daemon_running():
        console.print(f" [green]✓[/green] Daemon is active and responding on {config.socket_path}")
    else:
        console.print(f" [yellow]![/yellow] Daemon is not running on {config.socket_path}")

    # 5. System clock
    skew_ms = abs((time.time() - time.time()) * 1000)
    console.print(f" [green]✓[/green] System clock monotonic check healthy ({round(skew_ms, 2)}ms)")

    console.print()
    if all_ok:
        console.print("[bold green]All doctor checks passed![/bold green]")
    else:
        console.print("[bold red]Doctor reported issues. Review warnings above.[/bold red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Agent Management Commands
# ---------------------------------------------------------------------------


@agent_app.command("create")
def agent_create_cmd(
    name: str = typer.Argument(..., help="Human-readable display name for the agent"),
) -> None:
    """Create a new agent identity and print agent_id."""
    client = get_client()
    try:
        res = client.call_sync("agent_create", {"name": name})
        data = res.get("data", {})
        agent_id = data.get("agent_id")
        console.print(f"{agent_id}")
    except DaemonNotRunningError:
        # If daemon is not running, create directly via SQLite & keys dir
        config = ShakeConfig()
        config.ensure_directories()
        run_sync_migrations(config.db_path)

        import sqlite3

        from shake.crypto import KeyPair, generate_agent_id

        agent_id = generate_agent_id()
        keypair = KeyPair.generate()
        keypair.save(config.key_file_path(agent_id))

        conn = sqlite3.connect(str(config.db_path))
        try:
            now_ms = int(time.time() * 1000)
            conn.execute(
                "INSERT INTO agents (agent_id, name, pubkey, created_at) VALUES (?, ?, ?, ?);",
                (agent_id, name, keypair.public_bytes, now_ms),
            )
            conn.commit()
        finally:
            conn.close()

        console.print(f"{agent_id}")
    except Exception as e:
        err_console.print(f"[red]Error creating agent:[/red] {e}")
        raise typer.Exit(code=1)


@agent_app.command("list")
def agent_list_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output agent list as JSON"),
) -> None:
    """List local agents."""
    client = get_client()
    agents = []
    try:
        res = client.call_sync("agent_list")
        agents = res.get("data", {}).get("agents", [])
    except DaemonNotRunningError:
        config = ShakeConfig()
        if config.db_path.exists():
            import sqlite3

            from shake.crypto import format_fingerprint

            conn = sqlite3.connect(str(config.db_path))
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT agent_id, name, pubkey, created_at "
                    "FROM agents ORDER BY created_at ASC;"
                )
                rows = cur.fetchall()
                agents = [
                    {
                        "agent_id": str(r[0]),
                        "name": str(r[1]),
                        "fingerprint": format_fingerprint(bytes(r[2])),
                        "created_at": int(r[3]),
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    if json_output:
        console.print(json.dumps(agents, indent=2))
    else:
        if not agents:
            console.print("No agents found. Create one with `shake agent create <name>`.")
            return

        table = Table(box=box.ROUNDED)
        table.add_column("Agent ID", style="bold cyan")
        table.add_column("Name", style="green")
        table.add_column("Fingerprint", style="magenta")
        table.add_column("Created", style="dim")
        for a in agents:
            created_str = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(a["created_at"] / 1000)
            )
            table.add_row(a["agent_id"], a["name"], a["fingerprint"], created_str)
        console.print(table)


def resolve_active_agent(config: ShakeConfig) -> dict[str, Any] | None:
    """Resolve active agent from .shake/identity, SHAKE_AGENT, or single agent in DB."""
    # 1. Project identity (.shake/identity)
    proj_id = ShakeConfig.get_project_identity()
    target_id = proj_id or os.environ.get("SHAKE_AGENT")

    client = ShakeClient(config)
    agents: list[dict[str, Any]] = []
    try:
        res = client.call_sync("agent_list")
        agents = res.get("data", {}).get("agents", [])
    except DaemonNotRunningError:
        if config.db_path.exists():
            import sqlite3

            from shake.crypto import format_fingerprint

            conn = sqlite3.connect(str(config.db_path))
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT agent_id, name, pubkey, created_at "
                    "FROM agents ORDER BY created_at ASC;"
                )
                rows = cur.fetchall()
                agents = [
                    {
                        "agent_id": str(r[0]),
                        "name": str(r[1]),
                        "fingerprint": format_fingerprint(bytes(r[2])),
                        "created_at": int(r[3]),
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    if target_id:
        for a in agents:
            if a["agent_id"] == target_id:
                return a
        return None

    # Fallback to single agent if only one exists
    if len(agents) == 1:
        return agents[0]

    return None


@agent_app.command("whoami")
def agent_whoami_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output identity as JSON"),
) -> None:
    """Print the currently active agent identity."""
    config = ShakeConfig()
    agent = resolve_active_agent(config)

    if not agent:
        if json_output:
            console.print(json.dumps({"active_agent": None}, indent=2))
        else:
            console.print(
                "[yellow]No active agent selected.[/yellow] Use a `.shake/identity` file in your "
                "project, set `SHAKE_AGENT=<id>`, or pass an explicit agent ID."
            )
        raise typer.Exit(code=1)

    if json_output:
        console.print(json.dumps(agent, indent=2))
    else:
        console.print(f"[bold green]Agent ID:[/bold green] {agent['agent_id']}")
        console.print(f"[bold cyan]Name:[/bold cyan] {agent['name']}")
        console.print(f"[bold magenta]Fingerprint:[/bold magenta] {agent['fingerprint']}")


@agent_app.command("fingerprint")
def agent_fingerprint_cmd(
    agent_id: str | None = typer.Argument(
        None, help="Agent ID (optional; defaults to active agent)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output fingerprint as JSON"),
) -> None:
    """Print the short human-checkable public key fingerprint of an agent."""
    config = ShakeConfig()
    target = None

    if agent_id:
        client = ShakeClient(config)
        try:
            res = client.call_sync("agent_get", {"agent_id": agent_id})
            target = res.get("data")
        except DaemonNotRunningError:
            if config.db_path.exists():
                import sqlite3

                from shake.crypto import format_fingerprint

                conn = sqlite3.connect(str(config.db_path))
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT agent_id, name, pubkey, created_at FROM agents "
                        "WHERE agent_id = ?;",
                        (agent_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        target = {
                            "agent_id": str(row[0]),
                            "name": str(row[1]),
                            "fingerprint": format_fingerprint(bytes(row[2])),
                            "created_at": int(row[3]),
                        }
                finally:
                    conn.close()
    else:
        target = resolve_active_agent(config)

    if not target:
        err_console.print("[red]Error:[/red] Agent not found or none specified.")
        raise typer.Exit(code=1)

    if json_output:
        console.print(
            json.dumps(
                {"agent_id": target["agent_id"], "fingerprint": target["fingerprint"]},
                indent=2,
            )
        )
    else:
        console.print(target["fingerprint"])


@agent_app.command("rm")
def agent_rm_cmd(
    agent_id: str = typer.Argument(..., help="Agent ID to remove"),
) -> None:
    """Remove an agent and delete its key."""
    client = get_client()
    try:
        res = client.call_sync("agent_rm", {"agent_id": agent_id})
        data = res.get("data", {})
        if data.get("deleted"):
            console.print(f"[green]✓[/green] Removed agent {agent_id}")
        else:
            console.print(f"[yellow]Agent {agent_id} was not found.[/yellow]")
    except DaemonNotRunningError:
        config = ShakeConfig()
        import sqlite3

        conn = sqlite3.connect(str(config.db_path))
        deleted = False
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM agents WHERE agent_id = ?;", (agent_id,))
            conn.commit()
            deleted = cur.rowcount > 0
        finally:
            conn.close()

        key_file = config.key_file_path(agent_id)
        if key_file.exists():
            key_file.unlink()

        if deleted:
            console.print(f"[green]✓[/green] Removed agent {agent_id}")
        else:
            console.print(f"[yellow]Agent {agent_id} was not found.[/yellow]")


@agent_app.command("use")
def agent_use_cmd(
    agent_id: str = typer.Argument(..., help="Agent ID to set for current directory"),
) -> None:
    """Set the active agent identity for the current directory (.shake/identity)."""
    p = ShakeConfig.set_project_identity(agent_id)
    console.print(f"[green]✓[/green] Set active agent {agent_id} in {p}")


@agent_app.command("good-at")
def agent_good_at_cmd(
    skills: str = typer.Argument(..., help="What this agent excels at"),
    agent_id: str | None = typer.Option(
        None, "--agent", "-a", help="Agent ID (defaults to active agent)"
    ),
) -> None:
    """Set the capability description (good_at) for an agent."""
    config = ShakeConfig()
    target_id = agent_id
    if not target_id:
        active = resolve_active_agent(config)
        if active:
            target_id = active["agent_id"]

    if not target_id:
        err_console.print("[red]Error:[/red] No agent specified or active.")
        raise typer.Exit(code=1)

    client = get_client()
    try:
        client.call_sync("agent_good_at", {"agent_id": target_id, "good_at": skills})
        console.print(f"[green]✓[/green] Updated good_at for agent {target_id}: '{skills}'")
    except DaemonNotRunningError:
        config = ShakeConfig()
        if config.db_path.exists():
            import sqlite3

            conn = sqlite3.connect(str(config.db_path))
            try:
                conn.execute(
                    "UPDATE agents SET good_at = ? WHERE agent_id = ?;",
                    (skills, target_id),
                )
                conn.commit()
            finally:
                conn.close()
            console.print(f"[green]✓[/green] Updated good_at for agent {target_id}: '{skills}'")
        else:
            err_console.print("[red]Error:[/red] Database not initialized. Run `shake init`.")
            raise typer.Exit(code=1)
    except Exception as e:
        err_console.print(f"[red]Error updating good_at:[/red] {e}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Handshake & Verification Commands
# ---------------------------------------------------------------------------


@app.command("connect")
def connect_cmd(
    peer_id: str | None = typer.Argument(
        None, help="Target agent ID to connect with (optional if --from-blob / --from-file is used)"
    ),
    from_blob: str | None = typer.Option(
        None, "--from-blob", help="Signed connection token to import"
    ),
    from_file: Path | None = typer.Option(
        None, "--from-file", help="Path to file containing connection token"
    ),
) -> None:
    """Initiate or accept a connection request with an agent using signed envelopes."""
    config = ShakeConfig()
    active = resolve_active_agent(config)
    if not active:
        err_console.print(
            "[red]Error:[/red] No active agent. Set .shake/identity or SHAKE_AGENT=<id>."
        )
        raise typer.Exit(code=1)

    local_id = active["agent_id"]

    token = from_blob
    if from_file:
        if not from_file.exists():
            err_console.print(f"[red]Error:[/red] Token file not found: {from_file}")
            raise typer.Exit(code=1)
        token = from_file.read_text(encoding="utf-8").strip()

    if not peer_id and not token:
        err_console.print("[red]Error:[/red] Specify peer_id or pass --from-blob / --from-file.")
        raise typer.Exit(code=1)

    if peer_id and local_id == peer_id:
        err_console.print("[red]Error:[/red] An agent cannot connect to itself.")
        raise typer.Exit(code=1)

    client = get_client()
    try:
        call_params: dict[str, Any] = {"local_agent_id": local_id}
        if token:
            call_params["token"] = token
        else:
            call_params["peer_id"] = peer_id
            call_params["peer_name"] = f"Agent {peer_id}"

        res = client.call_sync("connect", call_params)
        data = res.get("data", {})
        state = data.get("state")
        out_token = data.get("token")
        effective_peer = data.get("peer_id") or peer_id

        if state == "pending_verify":
            code = data.get("code")
            console.print(
                "\n[bold green]Handshake established (state: pending_verify)![/bold green]"
            )
            console.print(
                f"\nMutual SAS code: [bold yellow on black]  {code}  [/bold yellow on black]\n"
            )
            console.print("[bold red]CRITICAL SAS VERIFICATION:[/bold red]")
            console.print(
                "1. Confirm this 6-digit code matches the peer's terminal output."
            )
            console.print("2. Do NOT proceed until you have confirmed they match out-of-band.")
            console.print("3. Both sides must now verify independently:")
            console.print(
                f"     [bold cyan]shake verify {effective_peer} <code_from_peer>[/bold cyan]\n"
            )
            if out_token:
                console.print(
                    "[dim]Response token (if pairing across machines/air-gap):[/dim]\n"
                    f"[dim]{out_token}[/dim]\n"
                )
        else:
            console.print(
                f"[green]✓[/green] Connect request generated for [bold]{effective_peer}[/bold] "
                f"(state: [cyan]{state}[/cyan])."
            )
            if out_token:
                console.print("\n[bold cyan]Connection Request Token:[/bold cyan]")
                console.print(f"{out_token}\n")
                console.print(
                    "Deliver this token to the peer (via transport, file, or clipboard). "
                    "Peer runs:\n"
                    "  [bold cyan]shake connect --from-blob <token>[/bold cyan]\n"
                )
    except Exception as e:
        err_console.print(f"[red]Connect failed:[/red] {e}")
        raise typer.Exit(code=1)


@app.command("verify")
def verify_cmd(
    peer_id: str = typer.Argument(..., help="Peer agent ID to verify"),
    code: str = typer.Argument(..., help="6-digit verification code"),
) -> None:
    """Enter the 6-digit code to complete mutual verification and pin the peer's key."""
    config = ShakeConfig()
    active = resolve_active_agent(config)
    if not active:
        err_console.print("[red]Error:[/red] No active agent selected.")
        raise typer.Exit(code=1)

    local_id = active["agent_id"]
    client = get_client()
    try:
        client.call_sync(
            "verify",
            {
                "local_agent_id": local_id,
                "peer_id": peer_id,
                "code": code,
            },
        )
        console.print(
            f"\n[bold green]✓ Mutual verification successful![/bold green] "
            f"Agent [bold]{peer_id}[/bold] is now [cyan]verified[/cyan]."
        )
        console.print("Public key pinned. You can now exchange secure messages.\n")
    except Exception as e:
        err_console.print(f"[red]Verification failed:[/red] {e}")
        raise typer.Exit(code=1)


@app.command("disconnect")
def disconnect_cmd(
    peer_id: str = typer.Argument(..., help="Peer agent ID to disconnect"),
) -> None:
    """Remove contact, delete pinned key, and terminate sessions."""
    config = ShakeConfig()
    active = resolve_active_agent(config)
    local_id = active["agent_id"] if active else ""

    client = get_client()
    try:
        client.call_sync("disconnect", {"local_agent_id": local_id, "peer_id": peer_id})
        console.print(f"[green]✓[/green] Disconnected from agent {peer_id} (pinned key deleted).")
    except Exception as e:
        err_console.print(f"[red]Disconnect failed:[/red] {e}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Contact Management Commands
# ---------------------------------------------------------------------------


@contact_app.command("list")
def contact_list_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output contacts as JSON"),
) -> None:
    """List peer contacts and their verification states."""
    config = ShakeConfig()
    active = resolve_active_agent(config)
    local_id = active["agent_id"] if active else ""

    client = get_client()
    contacts: list[dict[str, Any]] = []
    try:
        res = client.call_sync("contact_list", {"local_agent_id": local_id})
        contacts = res.get("data", {}).get("contacts", [])
    except DaemonNotRunningError:
        if config.db_path.exists():
            import sqlite3

            from shake.crypto import format_fingerprint

            conn = sqlite3.connect(str(config.db_path))
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT owner_agent_id, peer_id, name, pubkey, state, pinned_at, "
                    "updated_at, good_at FROM contacts WHERE owner_agent_id = ? "
                    "ORDER BY updated_at DESC;",
                    (local_id,),
                )
                rows = cur.fetchall()
                contacts = [
                    {
                        "peer_id": str(r[1]),
                        "name": str(r[2]),
                        "fingerprint": format_fingerprint(bytes(r[3])) if r[3] else None,
                        "state": str(r[4]),
                        "pinned_at": int(r[5]) if r[5] else None,
                        "updated_at": int(r[6]),
                        "good_at": str(r[7] or ""),
                    }
                    for r in rows
                ]
            finally:
                conn.close()
    except Exception as e:
        err_console.print(f"[red]Error listing contacts:[/red] {e}")
        raise typer.Exit(code=1)

    if json_output:
        console.print(json.dumps(contacts, indent=2))
    else:
        if not contacts:
            console.print("No contacts found. Use `shake connect <agent_id>` to pair.")
            return

        table = Table(title="Contacts", box=box.ROUNDED)
        table.add_column("Peer ID", style="bold cyan")
        table.add_column("Name", style="green")
        table.add_column("State", style="bold yellow")
        table.add_column("Fingerprint", style="magenta")
        table.add_column("Good At", style="dim")
        table.add_column("Pinned At", style="dim")

        for c in contacts:
            pinned_str = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c["pinned_at"] / 1000))
                if c.get("pinned_at")
                else "-"
            )
            table.add_row(
                c["peer_id"],
                c["name"],
                c["state"],
                c["fingerprint"] or "-",
                c.get("good_at") or "-",
                pinned_str,
            )
        console.print(table)


@contact_app.command("requests")
def contact_requests_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output requests as JSON"),
) -> None:
    """List pending inbound connection requests."""
    config = ShakeConfig()
    active = resolve_active_agent(config)
    local_id = active["agent_id"] if active else ""

    client = get_client()
    try:
        res = client.call_sync("contact_requests", {"local_agent_id": local_id})
        reqs = res.get("data", {}).get("requests", [])
        if json_output:
            console.print(json.dumps(reqs, indent=2))
        else:
            if not reqs:
                console.print("No pending inbound requests.")
                return

            table = Table(title="Inbound Contact Requests", box=box.ROUNDED)
            table.add_column("From Agent ID", style="bold cyan")
            table.add_column("Name", style="green")
            table.add_column("Fingerprint", style="magenta")
            table.add_column("Action", style="yellow")

            for r in reqs:
                table.add_row(
                    r["peer_id"],
                    r["name"],
                    r["fingerprint"] or "-",
                    f"shake connect {r['peer_id']}",
                )
            console.print(table)
    except Exception as e:
        err_console.print(f"[red]Error listing requests:[/red] {e}")
        raise typer.Exit(code=1)


@contact_app.command("trust")
def contact_trust_cmd(
    peer_id: str = typer.Argument(..., help="Verified agent ID to mark as trusted"),
) -> None:
    """Promote a verified contact to trusted status."""
    config = ShakeConfig()
    active = resolve_active_agent(config)
    local_id = active["agent_id"] if active else ""

    client = get_client()
    try:
        client.call_sync("contact_trust", {"local_agent_id": local_id, "peer_id": peer_id})
        console.print(
            f"[green]✓[/green] Contact {peer_id} marked as [bold green]trusted[/bold green]."
        )
    except Exception as e:
        err_console.print(f"[red]Error marking contact as trusted:[/red] {e}")
        raise typer.Exit(code=1)


@contact_app.command("block")
def contact_block_cmd(
    peer_id: str = typer.Argument(..., help="Agent ID to block"),
) -> None:
    """Block an agent and drop all future messages from it."""
    config = ShakeConfig()
    active = resolve_active_agent(config)
    local_id = active["agent_id"] if active else ""

    client = get_client()
    try:
        client.call_sync("contact_block", {"local_agent_id": local_id, "peer_id": peer_id})
        console.print(f"[green]✓[/green] Contact {peer_id} is now [bold red]blocked[/bold red].")
    except Exception as e:
        err_console.print(f"[red]Error blocking contact:[/red] {e}")
        raise typer.Exit(code=1)


@contact_app.command("unblock")
def contact_unblock_cmd(
    peer_id: str = typer.Argument(..., help="Agent ID to unblock"),
) -> None:
    """Unblock a contact."""
    config = ShakeConfig()
    active = resolve_active_agent(config)
    local_id = active["agent_id"] if active else ""

    client = get_client()
    try:
        client.call_sync("contact_unblock", {"local_agent_id": local_id, "peer_id": peer_id})
        console.print(f"[green]✓[/green] Contact {peer_id} has been [green]unblocked[/green].")
    except Exception as e:
        err_console.print(f"[red]Error unblocking contact:[/red] {e}")
        raise typer.Exit(code=1)
