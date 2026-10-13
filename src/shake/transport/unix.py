"""Unix Domain Socket transport implementation for local daemon communication."""

import asyncio
import os
from pathlib import Path

from shake.protocol import pack_frame, unpack_frame_header
from shake.transport.base import MessageHandler, Transport


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    """Read a single length-prefixed frame from a stream reader."""
    try:
        header = await reader.readexactly(4)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    length = unpack_frame_header(header)
    if length == 0:
        return b""
    try:
        payload = await reader.readexactly(length)
        return payload
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None


async def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    """Write a single length-prefixed frame to a stream writer."""
    framed = pack_frame(payload)
    writer.write(framed)
    await writer.drain()


class UnixTransport(Transport):
    """Server-side Unix domain socket transport for shaked."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path: Path = socket_path
        self._server: asyncio.Server | None = None
        self._handler: MessageHandler | None = None
        self._active_connections: set[asyncio.StreamWriter] = set()
        self._is_running: bool = False

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        """Start listening on the Unix domain socket."""
        if self._is_running:
            return

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove stale socket file if it exists
        if self.socket_path.exists() or os.path.islink(self.socket_path):
            self.socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )
        # Ensure 0600 on socket so only owner can access
        try:
            self.socket_path.chmod(0o600)
        except OSError:
            pass

        self._is_running = True

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle incoming client connection reading frames continuously."""
        self._active_connections.add(writer)
        try:
            while self._is_running:
                payload = await read_frame(reader)
                if payload is None:
                    break
                if self._handler:
                    await self._handler(payload, writer)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._active_connections.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def send_to(self, target: str, payload: bytes) -> None:
        """Broadcast or route payload to active client connection."""

    async def stop(self) -> None:
        """Stop server and clean up socket."""
        self._is_running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Close all active connections
        for writer in list(self._active_connections):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._active_connections.clear()

        # Remove socket file
        if self.socket_path.exists() or os.path.islink(self.socket_path):
            try:
                self.socket_path.unlink()
            except OSError:
                pass


async def send_unix_ipc(
    socket_path: Path,
    request_bytes: bytes,
    timeout: float = 10.0,
) -> bytes:
    """Client utility to send an IPC request frame and await response frame."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(socket_path)),
        timeout=timeout,
    )
    try:
        await write_frame(writer, request_bytes)
        response_bytes = await asyncio.wait_for(read_frame(reader), timeout=timeout)
        if response_bytes is None:
            raise ConnectionError("Server closed connection without responding")
        return response_bytes
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
