"""diary_id → memos/<id> 映射持久化。

文件格式：{"mapping": {"4": "memos/abc123", ...}}
"""

from __future__ import annotations

import json
from pathlib import Path


class MemosMapping:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict[str, str]] = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("mapping"), dict):
                    return {"mapping": {str(k): str(v) for k, v in raw["mapping"].items()}}
            except Exception:
                pass
        return {"mapping": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, diary_id: int | str) -> str | None:
        return self._data["mapping"].get(str(diary_id))

    def set(self, diary_id: int | str, memo_name: str) -> None:
        self._data["mapping"][str(diary_id)] = memo_name
        self.save()

    def reverse(self) -> dict[str, str]:
        """memo_name → diary_id 反向索引。"""
        return {v: k for k, v in self._data["mapping"].items()}
