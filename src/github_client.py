"""GitHub REST API 客户端（使用 Git Data API 做多文件单次 commit）。

只封装本插件需要的几个端点：
- 读取文件
- 获取分支 HEAD 的 commit / tree sha
- 创建 blob / tree / commit / 更新 ref

所有方法都是 async，基于 aiohttp。
"""

from __future__ import annotations

import base64
from typing import Any

import aiohttp


class GithubError(Exception):
    pass


class GithubClient:
    def __init__(self, token: str, repo: str, branch: str):
        if not token:
            raise GithubError("未配置 github_token")
        if not repo or "/" not in repo:
            raise GithubError("github_repo 格式应为 owner/name")
        self.token = token
        self.repo = repo
        self.branch = branch
        self.api_base = f"https://api.github.com/repos/{repo}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "astrbot-plugin-mizuki-diary",
        }

    async def _request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        json_body: Any = None,
    ) -> dict[str, Any]:
        async with session.request(
            method, url, headers=self._headers(), json=json_body
        ) as r:
            text = await r.text()
            if r.status >= 400:
                raise GithubError(
                    f"GitHub API {method} {url} -> {r.status}: {text[:500]}"
                )
            if not text:
                return {}
            return await _loads(text)

    async def get_file(self, path: str) -> tuple[str, str]:
        """读取文件内容。返回 (content_utf8, blob_sha)。"""
        url = f"{self.api_base}/contents/{path}?ref={self.branch}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=self._headers()) as r:
                if r.status == 404:
                    raise GithubError(f"文件不存在: {path}")
                if r.status >= 400:
                    body = await r.text()
                    raise GithubError(f"获取文件失败 {r.status}: {body[:500]}")
                data = await r.json()
        content_b64 = data["content"]
        content = base64.b64decode(content_b64).decode("utf-8")
        return content, data["sha"]

    async def get_branch_head(self) -> tuple[str, str]:
        """返回 (commit_sha, tree_sha)。"""
        async with aiohttp.ClientSession() as s:
            ref = await self._request(
                s, "GET", f"{self.api_base}/git/ref/heads/{self.branch}"
            )
            commit_sha = ref["object"]["sha"]
            commit = await self._request(
                s, "GET", f"{self.api_base}/git/commits/{commit_sha}"
            )
            tree_sha = commit["tree"]["sha"]
        return commit_sha, tree_sha

    async def commit_files(
        self, files: list[dict[str, Any]], message: str
    ) -> str:
        """用 Git Data API 一次提交多个文件。

        files: [{"path": "...", "content": bytes}, ...]
        返回新 commit 的 sha。
        """
        if not files:
            raise GithubError("commit_files 调用时 files 为空")
        async with aiohttp.ClientSession() as s:
            ref = await self._request(
                s, "GET", f"{self.api_base}/git/ref/heads/{self.branch}"
            )
            parent_sha = ref["object"]["sha"]
            parent_commit = await self._request(
                s, "GET", f"{self.api_base}/git/commits/{parent_sha}"
            )
            base_tree = parent_commit["tree"]["sha"]

            tree_entries: list[dict[str, Any]] = []
            for f in files:
                blob = await self._request(
                    s,
                    "POST",
                    f"{self.api_base}/git/blobs",
                    json_body={
                        "content": base64.b64encode(f["content"]).decode("ascii"),
                        "encoding": "base64",
                    },
                )
                tree_entries.append(
                    {
                        "path": f["path"],
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob["sha"],
                    }
                )

            new_tree = await self._request(
                s,
                "POST",
                f"{self.api_base}/git/trees",
                json_body={"base_tree": base_tree, "tree": tree_entries},
            )
            new_commit = await self._request(
                s,
                "POST",
                f"{self.api_base}/git/commits",
                json_body={
                    "message": message,
                    "tree": new_tree["sha"],
                    "parents": [parent_sha],
                },
            )
            await self._request(
                s,
                "PATCH",
                f"{self.api_base}/git/refs/heads/{self.branch}",
                json_body={"sha": new_commit["sha"], "force": False},
            )
        return new_commit["sha"]


async def _loads(text: str) -> dict[str, Any]:
    import json

    return json.loads(text)
