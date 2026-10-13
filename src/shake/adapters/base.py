"""Base Adapter interface for agent runtimes."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from shake.protocol import Envelope


class Adapter(ABC):
    """Abstract interface that every agent runtime adapter implements."""

    @abstractmethod
    def handle(
        self, envelope: Envelope, context: dict[str, Any]
    ) -> AsyncIterator[str]:
        """Process incoming message envelope and stream or yield chunks."""
        ...

    @abstractmethod
    async def heartbeat(self) -> None:
        """Report agent alive status."""
        ...
