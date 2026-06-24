from __future__ import annotations

import json
import threading
from pathlib import Path


class ProcessedStore:
    """Simple dedupe store with best-effort persistence."""

    def __init__(self, path: str, max_keys: int = 15000) -> None:
        self._path = Path(path)
        self._max_keys = max_keys
        self._keys: set[str] = set()
        self._lock = threading.Lock()
        self._flush_disabled = False
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            keys = data.get("processed", [])
            if isinstance(keys, list):
                self._keys = {str(k) for k in keys}
        except Exception:
            self._keys = set()

    def _flush_unlocked(self) -> None:
        if self._flush_disabled:
            return
        keys = sorted(self._keys)
        if len(keys) > self._max_keys:
            keys = keys[-self._max_keys :]
            self._keys = set(keys)
        payload = {"processed": keys}

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            self._flush_disabled = True
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def contains(self, channel_id: int, message_id: int) -> bool:
        key = f"{channel_id}:{message_id}"
        with self._lock:
            return key in self._keys

    def max_message_id(self, channel_id: int) -> int | None:
        prefix = f"{channel_id}:"
        with self._lock:
            ids = [
                int(key.split(":", 1)[1])
                for key in self._keys
                if key.startswith(prefix)
            ]
        return max(ids) if ids else None

    def add(self, channel_id: int, message_id: int) -> bool:
        key = f"{channel_id}:{message_id}"
        with self._lock:
            if key in self._keys:
                return False
            self._keys.add(key)
            self._flush_unlocked()
            return True
