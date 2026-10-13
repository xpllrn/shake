"""Transport interface abstraction for local and network messaging."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

# Callback for receiving incoming payload: handler(payload: bytes, reply_channel: Any)
MessageHandler = Callable[[bytes, Any], Awaitable[None]]


class Transport(ABC):
    """Abstract Transport interface.

    Allows plugging in Unix Domain Sockets for single-machine messaging,
    or NATS / QUIC for distributed multi-host transport without altering
    envelope or routing logic.
    """

    @abstractmethod
    async def start(self) -> None:
        """Start listening or establish transport connection."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully terminate transport listeners and close connections."""

    @abstractmethod
    async def send_to(self, target: str, payload: bytes) -> None:
        """Send raw binary frame to a target endpoint or peer."""

    @abstractmethod
    def set_handler(self, handler: MessageHandler) -> None:
        """Set the callback handler for received payloads."""
