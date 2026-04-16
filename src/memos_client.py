"""memos REST API 客户端（API v1）。

封装本插件需要的端点：
- 上传附件（attachments）
- 创建备忘（memos）
- 修改备忘内容（updateMask=content[,visibility]）
- 设置备忘附件（SetMemoAttachments）

所有方法都是 async，基于 aiohttp。瞬时网络错误（断开、超时、5xx）带指数退避重试。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import aiohttp


class MemosError(Exception):
    pass


_RETRYABLE_EXC = (
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ClientPayloadError,
    asyncio.TimeoutError,
)
_RETRYABLE_STATUS = {500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 1.5


class MemosClient:
    def __init__(self, host: str, token: str, visibility: str = "PUBLIC"):
        if not host:
            raise MemosError("未配置 memos_host")
        if not token:
            raise MemosError("未配置 memos_access_token")
        self.host = host.rstrip("/")
        self.token = token
        self.visibility = (visibility or "PUBLIC").upper()
        self.api_base = f"{self.host}/api/v1"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "astrbot-plugin-mizuki-diary",
        }

    def _new_session(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=60, connect=15)
        connector = aiohttp.TCPConnector(
            limit=4, force_close=True, enable_cleanup_closed=True
        )
        return aiohttp.ClientSession(timeout=timeout, connector=connector)

    async def _request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with session.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                ) as r:
                    text = await r.text()
                    if r.status in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                        await asyncio.sleep(_BACKOFF_BASE ** attempt)
                        continue
                    if r.status >= 400:
                        raise MemosError(
                            f"memos API {method} {url} -> {r.status}: {text[:500]}"
                        )
                    if not text:
                        return {}
                    import json

                    return json.loads(text)
            except _RETRYABLE_EXC as e:
                last_exc = e
                if attempt >= _MAX_ATTEMPTS:
                    break
                await asyncio.sleep(_BACKOFF_BASE ** attempt)
        raise MemosError(
            f"memos API {method} {url} 连接失败（已重试 {_MAX_ATTEMPTS} 次）: {last_exc}"
        )

    async def create_attachment(
        self, filename: str, data: bytes, mime: str = "image/webp"
    ) -> str:
        """上传一个附件，返回 resource name（形如 attachments/<id>）。"""
        body = {
            "filename": filename,
            "type": mime,
            "content": base64.b64encode(data).decode("ascii"),
        }
        async with self._new_session() as s:
            resp = await self._request(
                s, "POST", f"{self.api_base}/attachments", json_body=body
            )
        name = resp.get("name")
        if not name:
            raise MemosError(f"memos 创建附件未返回 name：{resp}")
        return name

    async def create_memo(
        self,
        content: str,
        attachments: list[str] | None = None,
        visibility: str | None = None,
    ) -> str:
        """创建一条 memo，返回 resource name（形如 memos/<id>）。

        attachments: 形如 ["attachments/xxx", ...]。
        """
        body: dict[str, Any] = {
            "content": content,
            "visibility": (visibility or self.visibility).upper(),
            "state": "NORMAL",
        }
        if attachments:
            body["attachments"] = [{"name": a} for a in attachments]
        async with self._new_session() as s:
            resp = await self._request(
                s, "POST", f"{self.api_base}/memos", json_body=body
            )
        name = resp.get("name")
        if not name:
            raise MemosError(f"memos 创建失败未返回 name：{resp}")
        return name

    async def update_memo_content(
        self, memo_name: str, content: str, visibility: str | None = None
    ) -> None:
        """更新 memo 的 content（和可选的 visibility），使用 updateMask。"""
        fields = ["content"]
        body: dict[str, Any] = {"content": content}
        if visibility:
            body["visibility"] = visibility.upper()
            fields.append("visibility")
        params = {"updateMask": ",".join(fields)}
        async with self._new_session() as s:
            await self._request(
                s,
                "PATCH",
                f"{self.api_base}/{memo_name}",
                json_body=body,
                params=params,
            )

    async def set_memo_attachments(
        self, memo_name: str, attachments: list[str]
    ) -> None:
        """替换 memo 的附件列表。"""
        body = {"attachments": [{"name": a} for a in attachments]}
        async with self._new_session() as s:
            await self._request(
                s,
                "PATCH",
                f"{self.api_base}/{memo_name}/attachments",
                json_body=body,
            )

    async def update_memo_visibility(
        self, memo_name: str, visibility: str
    ) -> None:
        """仅更新 visibility。"""
        body = {"visibility": visibility.upper()}
        params = {"updateMask": "visibility"}
        async with self._new_session() as s:
            await self._request(
                s,
                "PATCH",
                f"{self.api_base}/{memo_name}",
                json_body=body,
                params=params,
            )

    async def list_memos(self, page_size: int = 100) -> list[dict[str, Any]]:
        """列出所有 memos（自动分页）。"""
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        async with self._new_session() as s:
            while True:
                params: dict[str, str] = {"pageSize": str(page_size)}
                if page_token:
                    params["pageToken"] = page_token
                resp = await self._request(
                    s, "GET", f"{self.api_base}/memos", params=params
                )
                out.extend(resp.get("memos", []) or [])
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return out

    async def download_attachment(
        self, attachment_name: str, filename: str
    ) -> bytes:
        """下载附件二进制。走 memos 的 file 端点。"""
        url = f"{self.host}/file/{attachment_name}/{filename}"
        last_exc: Exception | None = None
        async with self._new_session() as s:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    async with s.get(url, headers=self._headers()) as r:
                        if r.status in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                            await asyncio.sleep(_BACKOFF_BASE ** attempt)
                            continue
                        if r.status >= 400:
                            raise MemosError(
                                f"下载附件 {url} -> {r.status}"
                            )
                        return await r.read()
                except _RETRYABLE_EXC as e:
                    last_exc = e
                    if attempt >= _MAX_ATTEMPTS:
                        break
                    await asyncio.sleep(_BACKOFF_BASE ** attempt)
        raise MemosError(
            f"下载附件 {url} 失败（已重试 {_MAX_ATTEMPTS} 次）: {last_exc}"
        )
