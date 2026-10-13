"""Client library for interacting with the local shaked daemon over Unix domain socket."""

from typing import Any

from shake.config import ShakeConfig
from shake.protocol import IpcRequest, IpcResponse
from shake.transport.unix import send_unix_ipc
from shake.ulid import generate_ulid


class ShakeClientError(Exception):
    """Error communicating with or returned by shaked daemon."""


class DaemonNotRunningError(ShakeClientError):
    """Raised when the shaked daemon socket cannot be reached."""


class ShakeClient:
    """Synchronous / Asynchronous helper client to talk to shaked."""

    def __init__(self, config: ShakeConfig | None = None) -> None:
        self.config = config or ShakeConfig()

    def is_daemon_running(self) -> bool:
        """Check if daemon is currently responding to ping."""
        if not self.config.socket_path.exists():
            return False
        try:
            res = self.call_sync("ping")
            return res.get("data") == "pong"
        except Exception:
            return False

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Send an IPC request asynchronously to shaked."""
        if not self.config.socket_path.exists():
            raise DaemonNotRunningError(
                f"shaked daemon is not running (socket not found: {self.config.socket_path}). "
                "Start it with `shake start`."
            )

        req = IpcRequest(
            id=generate_ulid(),
            action=action,
            params=params or {},
        )

        try:
            resp_bytes = await send_unix_ipc(
                self.config.socket_path,
                req.encode(),
                timeout=timeout,
            )
        except (ConnectionRefusedError, FileNotFoundError, ConnectionError) as e:
            raise DaemonNotRunningError(f"Cannot connect to shaked: {e}")

        resp = IpcResponse.decode(resp_bytes)
        if not resp.ok:
            raise ShakeClientError(resp.error or "Unknown daemon error")
        return {"id": resp.id, "data": resp.data}

    def call_sync(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Synchronously execute call by running an event loop."""
        import asyncio

        return asyncio.run(self.call(action, params, timeout=timeout))
