"""Bounded, atomic persistence for MQTT payload history."""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HISTORY_LIMIT = 200
MAX_PAYLOAD_CHARACTERS = 16 * 1024
# JSON escaping can expand a control character to six bytes. This ceiling still
# bounds disk reads while accommodating 200 maximally escaped 16 KiB payloads.
MAX_HISTORY_BYTES = HISTORY_LIMIT * MAX_PAYLOAD_CHARACTERS * 6 + 64 * 1024

_history_lock = threading.Lock()


def _valid_entries(value: Any) -> list[dict[str, Any]]:
    """Return only records matching the small contract consumed by the website."""
    if not isinstance(value, list):
        return []
    return [
        entry
        for entry in value
        if isinstance(entry, dict)
        and isinstance(entry.get("received_at"), str)
        and "payload" in entry
    ]


def read_payload_history(path: Path) -> list[dict[str, Any]]:
    """Read a bounded history file without following a web-visible symlink."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_HISTORY_BYTES:
            return []
        with path.open(encoding="utf-8") as history_file:
            return _valid_entries(json.load(history_file))[-HISTORY_LIMIT:]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []


def record_payload(path: Path, payload_text: str, received_at: str | None = None) -> None:
    """Append one received payload and atomically replace the bounded JSON cache."""
    truncated = len(payload_text) > MAX_PAYLOAD_CHARACTERS
    stored_text = payload_text[:MAX_PAYLOAD_CHARACTERS]
    try:
        payload = json.loads(stored_text) if not truncated else stored_text
    except json.JSONDecodeError:
        payload = stored_text

    entry: dict[str, Any] = {
        "received_at": received_at or datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    if truncated:
        # An explicit marker prevents the UI from presenting an incomplete value
        # as a payload that is safe to replay.
        entry["truncated"] = True

    temporary_path: Path | None = None
    with _history_lock:
        entries = read_payload_history(path)
        entries = (entries + [entry])[-HISTORY_LIMIT:]
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False
            ) as temporary:
                json.dump(entries, temporary, ensure_ascii=False, separators=(",", ":"))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
        finally:
            # os.replace removes the temporary name on success; this only cleans
            # up an interrupted or failed write while preserving the old cache.
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
