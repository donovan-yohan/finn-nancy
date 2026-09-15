from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Status = Literal["PASS", "NEEDS_ADAPTER", "BLOCKED", "FAIL"]


@dataclass
class SmokeResult:
    primitive: str
    status: Status
    note: str
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
