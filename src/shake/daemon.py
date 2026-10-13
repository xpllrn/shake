"""shaked - Local daemon managing Unix domain socket, routing, and agents."""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from shake.config import ShakeConfig
from shake.crypto import KeyPair, format_fingerprint, generate_agent_id
from shake.delivery import DeliveryManager
from shake.pairing import PairingManager
from shake.protocol import IpcRequest, IpcResponse
from shake.store import Store
from shake.transport.unix import UnixTransport, write_frame

logger = logging.getLogger("shaked")


class Daemon:
    """Shake background daemon engine."""

    def __init__(self, config: ShakeConfig | None = None) -> None:
        self.config = config or ShakeConfig()
        self.store = Store(self.config.db_path)
        self.pairing = PairingManager(self.store, self.config)
        self.delivery = DeliveryManager(self.store, self.config)
        self.transport = UnixTransport(self.config.socket_path)
        self.start_time: float = 0.0
        self._stop_event = asyncio.Event()

    async def handle_ipc(self, payload: bytes, reply_channel: asyncio.StreamWriter) -> None:
        """Handle incoming IPC message frame from CLI or adapter."""
        try:
            req = IpcRequest.decode(payload)
        except Exception as e:
            logger.error("Failed to decode IPC request: %s", e)
            err_resp = IpcResponse(id="unknown", ok=False, error=f"Decode error: {e}")
            await write_frame(reply_channel, err_resp.encode())
            return

        action = req.action
        params = req.params
        req_id = req.id

        try:
            if action == "ping":
                resp = IpcResponse(id=req_id, ok=True, data="pong")

            elif action == "status":
                agents = await self.store.list_agents()
                uptime = time.time() - self.start_time if self.start_time else 0
                resp = IpcResponse(
                    id=req_id,
                    ok=True,
                    data={
                        "pid": os.getpid(),
                        "uptime_seconds": round(uptime, 2),
                        "socket": str(self.config.socket_path),
                        "db": str(self.config.db_path),
                        "agents_count": len(agents),
                    },
                )

            elif action == "stop":
                resp = IpcResponse(id=req_id, ok=True, data={"stopped": True})
                await write_frame(reply_channel, resp.encode())
                # Trigger stop event
                self._stop_event.set()
                return

            elif action == "agent_create":
                name = str(params.get("name", "")).strip()
                if not name:
                    resp = IpcResponse(id=req_id, ok=False, error="Agent name is required")
                else:
                    # Generate unique 7-digit ID
                    while True:
                        cand_id = generate_agent_id()
                        existing = await self.store.get_agent(cand_id)
                        if not existing:
                            agent_id = cand_id
                            break

                    keypair = KeyPair.generate()
                    key_path = self.config.key_file_path(agent_id)
                    keypair.save(key_path)

                    record = await self.store.create_agent(
                        agent_id=agent_id,
                        name=name,
                        pubkey=keypair.public_bytes,
                    )
                    resp = IpcResponse(
                        id=req_id,
                        ok=True,
                        data={
                            "agent_id": record.agent_id,
                            "name": record.name,
                            "fingerprint": keypair.fingerprint(),
                            "created_at": record.created_at,
                        },
                    )

            elif action == "agent_list":
                agents = await self.store.list_agents()
                agents_data = [
                    {
                        "agent_id": a.agent_id,
                        "name": a.name,
                        "fingerprint": format_fingerprint(a.pubkey),
                        "created_at": a.created_at,
                    }
                    for a in agents
                ]
                resp = IpcResponse(id=req_id, ok=True, data={"agents": agents_data})

            elif action == "agent_get":
                agent_id = str(params.get("agent_id", "")).strip()
                agent = await self.store.get_agent(agent_id)
                if not agent:
                    resp = IpcResponse(id=req_id, ok=False, error=f"Agent '{agent_id}' not found")
                else:
                    resp = IpcResponse(
                        id=req_id,
                        ok=True,
                        data={
                            "agent_id": agent.agent_id,
                            "name": agent.name,
                            "fingerprint": format_fingerprint(agent.pubkey),
                            "created_at": agent.created_at,
                        },
                    )

            elif action == "agent_rm":
                agent_id = str(params.get("agent_id", "")).strip()
                deleted = await self.store.delete_agent(agent_id)
                key_path = self.config.key_file_path(agent_id)
                if key_path.exists():
                    try:
                        key_path.unlink()
                    except OSError:
                        pass
                resp = IpcResponse(
                    id=req_id,
                    ok=True,
                    data={"deleted": deleted, "agent_id": agent_id},
                )

            elif action == "connect":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                token = params.get("token")
                if token:
                    result = await self.pairing.process_connect_token(
                        local_agent_id=local_agent_id,
                        token=str(token),
                    )
                else:
                    peer_id = str(params.get("peer_id", "")).strip()
                    peer_name = str(params.get("peer_name", "")).strip()
                    result = await self.pairing.initiate_connect(
                        local_agent_id=local_agent_id,
                        peer_id=peer_id,
                        peer_name=peer_name,
                    )
                resp = IpcResponse(id=req_id, ok=True, data=result)

            elif action == "verify":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                peer_id = str(params.get("peer_id", "")).strip()
                code = str(params.get("code", "")).strip()

                verify_ok = await self.pairing.verify_code(
                    local_agent_id=local_agent_id,
                    peer_id=peer_id,
                    code=code,
                )
                resp = IpcResponse(id=req_id, ok=True, data={"verified": verify_ok})

            elif action == "disconnect":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                peer_id = str(params.get("peer_id", "")).strip()
                success = await self.pairing.disconnect(local_agent_id, peer_id)
                resp = IpcResponse(
                    id=req_id,
                    ok=True,
                    data={"disconnected": success, "peer_id": peer_id},
                )

            elif action == "contact_list":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                state_filter = params.get("state")
                contacts = await self.store.list_contacts(
                    owner_agent_id=local_agent_id,
                    state=str(state_filter) if state_filter else None,
                )
                contacts_data = [
                    {
                        "peer_id": c.peer_id,
                        "name": c.name,
                        "state": c.state,
                        "fingerprint": format_fingerprint(c.pubkey) if c.pubkey else "",
                        "pinned_at": c.pinned_at,
                        "updated_at": c.updated_at,
                        "good_at": c.good_at,
                    }
                    for c in contacts
                ]
                resp = IpcResponse(id=req_id, ok=True, data={"contacts": contacts_data})

            elif action == "contact_requests":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                contacts = await self.store.list_contacts(
                    owner_agent_id=local_agent_id, state="requested_in"
                )
                contacts_data = [
                    {
                        "peer_id": c.peer_id,
                        "name": c.name,
                        "state": c.state,
                        "fingerprint": format_fingerprint(c.pubkey) if c.pubkey else "",
                        "updated_at": c.updated_at,
                        "good_at": c.good_at,
                    }
                    for c in contacts
                ]
                resp = IpcResponse(id=req_id, ok=True, data={"requests": contacts_data})

            elif action == "contact_trust":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                peer_id = str(params.get("peer_id", "")).strip()
                success = await self.pairing.trust(local_agent_id, peer_id)
                resp = IpcResponse(
                    id=req_id, ok=True, data={"trusted": success, "peer_id": peer_id}
                )

            elif action == "contact_block":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                peer_id = str(params.get("peer_id", "")).strip()
                success = await self.pairing.block(local_agent_id, peer_id)
                resp = IpcResponse(
                    id=req_id, ok=True, data={"blocked": success, "peer_id": peer_id}
                )

            elif action == "contact_unblock":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                peer_id = str(params.get("peer_id", "")).strip()
                success = await self.pairing.unblock(local_agent_id, peer_id)
                resp = IpcResponse(
                    id=req_id, ok=True, data={"unblocked": success, "peer_id": peer_id}
                )

            elif action == "agent_good_at":
                agent_id = str(params.get("agent_id", "")).strip()
                good_at = str(params.get("good_at", "")).strip()
                success = await self.store.update_agent_good_at(agent_id, good_at)
                resp = IpcResponse(
                    id=req_id, ok=True, data={"updated": success, "good_at": good_at}
                )

            elif action == "contact_good_at":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                peer_id = str(params.get("peer_id", "")).strip()
                good_at = str(params.get("good_at", "")).strip()
                success = await self.store.update_contact_good_at(
                    owner_agent_id=local_agent_id, peer_id=peer_id, good_at=good_at
                )
                resp = IpcResponse(
                    id=req_id, ok=True, data={"updated": success, "good_at": good_at}
                )

            elif action == "text":
                sender_id = str(params.get("sender_id", "")).strip()
                target_id = str(params.get("target_id", "")).strip()
                body = str(params.get("body", ""))
                wait_timeout = float(params.get("wait_timeout", 60.0))
                check_presence = bool(params.get("check_presence", False))

                sent_env, ack_env = await self.delivery.send_text(
                    sender_id=sender_id,
                    target_id=target_id,
                    body=body,
                    wait_timeout=wait_timeout,
                    check_presence=check_presence,
                )
                resp = IpcResponse(
                    id=req_id,
                    ok=True,
                    data={
                        "msg_id": sent_env.msg_id,
                        "status": "acked",
                        "ack_msg_id": ack_env.msg_id,
                        "ts": sent_env.ts,
                    },
                )

            elif action == "inbox":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                after_msg_id = params.get("after_msg_id")
                limit = int(params.get("limit", 50))
                messages = await self.store.get_inbox(
                    owner_agent_id=local_agent_id,
                    after_msg_id=str(after_msg_id) if after_msg_id else None,
                    limit=limit,
                )
                resp = IpcResponse(
                    id=req_id,
                    ok=True,
                    data={
                        "messages": [
                            {
                                "msg_id": m.msg_id,
                                "sender_id": m.sender_id,
                                "target_id": m.target_id,
                                "ts": m.ts,
                                "state": m.state,
                                "reply_to": m.reply_to,
                                "body": m.body,
                            }
                            for m in messages
                        ]
                    },
                )

            elif action == "history":
                local_agent_id = str(params.get("local_agent_id", "")).strip()
                peer_id = str(params.get("peer_id", "")).strip()
                limit = int(params.get("limit", 50))
                messages = await self.store.get_history(
                    owner_agent_id=local_agent_id,
                    peer_id=peer_id,
                    limit=limit,
                )
                resp = IpcResponse(
                    id=req_id,
                    ok=True,
                    data={
                        "messages": [
                            {
                                "msg_id": m.msg_id,
                                "direction": m.direction,
                                "sender_id": m.sender_id,
                                "target_id": m.target_id,
                                "ts": m.ts,
                                "state": m.state,
                                "reply_to": m.reply_to,
                                "body": m.body,
                            }
                            for m in messages
                        ]
                    },
                )

            elif action == "heartbeat":
                agent_id = str(params.get("agent_id", "")).strip()
                status = str(params.get("status", "online"))
                metadata = params.get("metadata")
                await self.store.record_heartbeat(
                    agent_id=agent_id,
                    status=status,
                    metadata=str(metadata) if metadata else None,
                )
                resp = IpcResponse(
                    id=req_id,
                    ok=True,
                    data={"heartbeat": "recorded", "agent_id": agent_id},
                )

            else:
                resp = IpcResponse(id=req_id, ok=False, error=f"Unknown action: {action}")

        except Exception as e:
            logger.exception("Error executing action %s", action)
            resp = IpcResponse(id=req_id, ok=False, error=str(e))

        await write_frame(reply_channel, resp.encode())

    def stop(self) -> None:
        """Trigger graceful shutdown of the daemon."""
        self._stop_event.set()

    async def run(self) -> None:
        """Run daemon server until stopped."""
        self.config.ensure_directories()
        self.start_time = time.time()

        # Write PID file
        self.config.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        try:
            self.config.pid_path.chmod(0o600)
        except OSError:
            pass

        # Open store
        await self.store.open()

        # Setup transport
        self.transport.set_handler(self.handle_ipc)
        await self.transport.start()
        logger.info("shaked listening on %s (pid %s)", self.config.socket_path, os.getpid())

        # Setup OS signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except (NotImplementedError, RuntimeError, ValueError):
                # Signal handlers not implemented on Windows or outside main thread
                pass

        try:
            await self._stop_event.wait()
        finally:
            logger.info("shaked shutting down...")
            await self.transport.stop()
            await self.store.close()
            if self.config.pid_path.exists():
                try:
                    self.config.pid_path.unlink()
                except OSError:
                    pass
            logger.info("shaked stopped.")


def setup_daemon_logging(log_path: Path, verbose: bool = False) -> None:
    """Configure logging to file and standard error."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(str(log_path), encoding="utf-8"),
    ]
    if not verbose:
        handlers.append(logging.StreamHandler(sys.stderr))

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
        handlers=handlers,
    )


def main() -> None:
    """Entry point for `shaked` executable."""
    config = ShakeConfig()
    setup_daemon_logging(config.log_path)
    daemon = Daemon(config)
    try:
        asyncio.run(daemon.run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
