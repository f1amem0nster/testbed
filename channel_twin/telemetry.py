"""Append-only event logging for the standalone channel twin."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonlEventLogger:
    """Write one compact, UTF-8 JSON object per line."""

    def __init__(self, path: str | Path, node_id: str, role: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.node_id = node_id
        self.role = role
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "timestamp": _utc_now(),
            "node_id": self.node_id,
            "role": self.role,
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
