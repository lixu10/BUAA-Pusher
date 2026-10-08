from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ConnectorManifest:
    """Versioned, inspectable contract for one upstream school system."""

    id: str
    label: str
    version: str
    capabilities: tuple[str, ...]
    endpoints: tuple[str, ...]
    references: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return asdict(self)


class SourceAdapter(ABC):
    id: str
    label: str
    manifest: ConnectorManifest

    def report_progress(self, completed: int, total: int, phase: str = "读取数据") -> None:
        callback = getattr(self, "on_progress", None)
        if callback:
            callback({"completed": completed, "total": total, "phase": phase})

    @abstractmethod
    async def sync(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def close(self) -> None:
        return None
