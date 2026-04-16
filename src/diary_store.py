"""pending.json 本地暂存 —— 存放尚未推送到 GitHub 的变更（patches）。

patch 结构：

    {"op": "add",     "item": {...full item...}, "image_files": [{local_path, remote_path}, ...]}
    {"op": "edit",    "id": N, "fields": {...},  "image_files": [...]}   # image_files 可选
    {"op": "delete",  "id": N}
    {"op": "restore", "id": N}

apply_patches 将 patches 叠加到从 GitHub 拉取的 remote_items 上，产出当前工作视图。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DiaryStore:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"patches": []}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @property
    def patches(self) -> list[dict[str, Any]]:
        return self._data["patches"]

    def add_patch(self, patch: dict[str, Any]) -> None:
        self._data["patches"].append(patch)
        self.save()

    def clear(self) -> None:
        self._data["patches"] = []
        self.save()

    def has_pending(self) -> bool:
        return len(self._data["patches"]) > 0

    def summary(self) -> dict[str, int]:
        counts = {"add": 0, "edit": 0, "delete": 0, "restore": 0}
        for p in self._data["patches"]:
            op = p.get("op")
            if op in counts:
                counts[op] += 1
        return counts

    def collect_image_files(self) -> list[dict[str, str]]:
        """收集所有 patch 中的待上传图片。"""
        out: list[dict[str, str]] = []
        for p in self._data["patches"]:
            for f in p.get("image_files", []) or []:
                out.append(f)
        return out


def apply_patches(
    remote_items: list[dict[str, Any]],
    patches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 patches 按顺序叠加到 remote_items 上，返回当前工作视图。"""
    items = [dict(it) for it in remote_items]
    idx_by_id: dict[int, int] = {
        it["id"]: i for i, it in enumerate(items) if "id" in it
    }
    for p in patches:
        op = p.get("op")
        if op == "add":
            new_item = dict(p["item"])
            new_item.setdefault("_deleted", False)
            items.append(new_item)
            if "id" in new_item:
                idx_by_id[new_item["id"]] = len(items) - 1
        elif op == "edit":
            i = idx_by_id.get(p["id"])
            if i is None:
                continue
            items[i].update(p["fields"])
        elif op == "delete":
            i = idx_by_id.get(p["id"])
            if i is None:
                continue
            items[i]["_deleted"] = True
        elif op == "restore":
            i = idx_by_id.get(p["id"])
            if i is None:
                continue
            items[i]["_deleted"] = False
    return items
