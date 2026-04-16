"""Mizuki Diary 插件 —— QQ 指令远程管理博客仓库的 data/diary.ts。

多轮对话通过 astrbot 的 session_waiter 实现：add / edit 用状态机在同一会话内推进，
push 用单步确认。所有改动先写入 pending.json，确认后通过 GitHub Git Data API
一次性提交（diary.ts + 新增图片同属一个 commit）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .src import diary_parser, image_utils
from .src.diary_store import DiaryStore, apply_patches
from .src.github_client import GithubClient, GithubError
from .src.image_utils import ImageError
from .src.memos_client import MemosClient, MemosError
from .src.memos_mapping import MemosMapping

PLUGIN_NAME = "astrbot_plugin_mizuki_diary"

CANCEL_TOKENS = {"cancel", "取消"}
DONE_TOKENS = {"done", "完成"}
SKIP_TOKENS = {"skip", "跳过"}
CLEAR_TOKENS = {"clear", "清空"}

DONE_DISPLAY = "/diary done"
CANCEL_DISPLAY = "/diary cancel"

HELP_TEXT = (
    "Mizuki Diary 指令：\n"
    "/diary help                查看帮助\n"
    "/diary list [page]         列出日记（含已删除）\n"
    "/diary preview <id>        预览一条（含图片）\n"
    "/diary quick [文字]        快速新增（文字 + 图片两步）\n"
    "/diary add                 新增（完整多轮对话）\n"
    "/diary edit <id>           修改（字段菜单）\n"
    "/diary del <id>            删除（注释掉）\n"
    "/diary restore <id>        恢复已删除\n"
    "/diary diff                查看本地 pending 改动\n"
    "/diary discard             放弃所有 pending\n"
    "/diary push                推送到 GitHub（二次确认）\n"
    "/diary sync memos          把未映射的日记批量同步到 memos\n"
    "/diary cancel              取消当前多轮对话\n"
    "\nquick 的首行可加 # 字段头：#地点:xxx #心情:xxx #标签:a,b #日期:YYYY-MM-DD HH:mm:ss"
    "\n多轮对话内：/diary done 结束当前步骤；skip/跳过 跳过可选项；/diary cancel 取消。"
)


def _normalize_token(s: str) -> str:
    """去掉前导 `/` 和 `diary ` 前缀，小写化。

    兼容多种 AstrBot 前缀处理方式：
    - "/diary done"   → "done"
    - "diary done"    → "done"  (AstrBot 剥掉 /)
    - "/done"         → "done"
    - "done"          → "done"
    - "  DONE  "      → "done"
    """
    t = s.strip().lower()
    while t.startswith("/"):
        t = t[1:]
    if t.startswith("diary "):
        t = t[6:].strip()
    return t


def _is_token(text: str, tokens: set[str]) -> bool:
    t = _normalize_token(text)
    if not t:
        return False
    return t in {_normalize_token(x) for x in tokens}


def _looks_like_slash_cmd(text: str) -> bool:
    """检测用户是否试图发送 / 开头的指令（原始文本或剥离后都以 `diary` 开头）。"""
    t = text.strip().lower()
    return t.startswith("/") or t.startswith("diary ")


def _parse_meta_line(s: str) -> tuple[str, str, bool]:
    """从 '📍 xxx 💭 yyy' 这种单行里拆出 location/mood。

    返回 (location, mood, matched)；matched=True 表示至少识别到一个表情头。
    兼容只有一项、或表情在任意位置的情况，但要求整行只含这 0-2 段内容。
    """
    import re

    if "\n" in s:
        return "", "", False
    pin_re = re.compile(r"📍\s*([^💭]+?)(?=\s*💭|$)")
    mood_re = re.compile(r"💭\s*([^📍]+?)(?=\s*📍|$)")
    loc = ""
    mood = ""
    matched = False
    m = pin_re.search(s)
    if m:
        loc = m.group(1).strip()
        matched = True
    m = mood_re.search(s)
    if m:
        mood = m.group(1).strip()
        matched = True
    # 确保整行除了这两个片段外没有其他内容
    if matched:
        remainder = s
        remainder = pin_re.sub("", remainder)
        remainder = mood_re.sub("", remainder)
        if remainder.replace("📍", "").replace("💭", "").strip():
            return "", "", False
    return loc, mood, matched


def _parse_tag_line(s: str) -> list[str] | None:
    """如果一行全是 '#tag1 #tag2' 形式则返回 tag 列表，否则 None。"""
    if not s:
        return None
    tokens = s.split()
    if not tokens:
        return None
    out: list[str] = []
    for tok in tokens:
        if not tok.startswith("#") or len(tok) < 2:
            return None
        out.append(tok[1:])
    return out


def _parse_iso8601(s: str):
    from datetime import datetime

    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_diary_date(s, tz):
    from datetime import datetime

    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=tz)
    except (ValueError, TypeError):
        return None


def _memo_display_date(memo: dict, tz) -> str | None:
    """取 memo 的 displayTime / createTime，格式化为 diary date 字符串。"""
    iso = memo.get("displayTime") or memo.get("createTime") or ""
    dt = _parse_iso8601(iso)
    if dt is None:
        return None
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def _event_has_payload(evt) -> bool:
    """判断事件是否包含可处理内容（文本或图片）。

    用来过滤 NapCat 的输入状态回调、心跳等空事件 —— 它们会被 session_waiter
    当成普通消息事件，导致 push 确认立刻被当作"其他回复"取消。
    """
    text = (getattr(evt, "message_str", None) or "").strip()
    if text:
        return True
    try:
        for seg in evt.get_messages():
            if isinstance(seg, Comp.Image):
                return True
    except Exception:
        pass
    return False


@register(
    PLUGIN_NAME,
    "olinll",
    "通过 QQ 指令远程管理 Mizuki 博客的日记数据",
    "0.1.0",
)
class MizukiDiaryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_root = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        data_root.mkdir(parents=True, exist_ok=True)
        self.data_root = data_root
        self.image_cache_dir = data_root / "image_cache"
        self.image_cache_dir.mkdir(exist_ok=True)
        self.store = DiaryStore(data_root / "pending.json")
        self.memos_map = MemosMapping(data_root / "memos_mapping.json")

    async def terminate(self):
        pass

    # ---------------------------------------------------------------- helpers

    def _client(self) -> GithubClient:
        return GithubClient(
            self.config.get("github_token", ""),
            self.config.get("github_repo", ""),
            self.config.get("github_branch", "master"),
        )

    def _memos_client(self) -> MemosClient | None:
        if not self.config.get("memos_enabled", False):
            return None
        return MemosClient(
            self.config.get("memos_host", ""),
            self.config.get("memos_access_token", ""),
            self.config.get("memos_visibility", "PUBLIC"),
        )

    @staticmethod
    def _format_memo_content(item: dict[str, Any]) -> str:
        """拼出 memos 正文：首行 #日记 标记、📍/💭 元数据、空行、正文、空行、#tag。

        首行 #日记 是反向同步（memos → mizuki）的过滤标记。
        """
        segments: list[str] = ["#日记"]
        meta_parts: list[str] = []
        if item.get("location"):
            meta_parts.append(f"📍 {item['location']}")
        if item.get("mood"):
            meta_parts.append(f"💭 {item['mood']}")
        if meta_parts:
            segments.append(" ".join(meta_parts))
        body = (item.get("content") or "").strip()
        if body:
            segments.append(body)
        tags = item.get("tags") or []
        if tags:
            segments.append(" ".join(f"#{t}" for t in tags))
        return "\n\n".join(segments)

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.config.get("timezone", "Asia/Shanghai"))
        except Exception:
            return ZoneInfo("Asia/Shanghai")

    def _now_str(self) -> str:
        return datetime.now(self._tz()).strftime("%Y-%m-%d %H:%M:%S")

    def _timeout(self) -> int:
        return int(self.config.get("session_timeout", 300))

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        wl = self.config.get("allowed_user_ids", []) or []
        return str(event.get_sender_id()) in [str(x) for x in wl]

    async def _current_view(self) -> tuple[str, list[dict[str, Any]]]:
        """拉远程 → 解析 → 叠加 pending patches → 返回 (raw_content, items)。"""
        client = self._client()
        content, _ = await client.get_file(self.config.get("diary_file_path", "data/diary.ts"))
        remote = diary_parser.parse(content)
        working = apply_patches(remote, self.store.patches)
        return content, working

    def _next_id(self, working: list[dict[str, Any]]) -> int:
        return max((int(it.get("id", 0)) for it in working), default=0) + 1

    def _collect_moods(self, working: list[dict[str, Any]]) -> list[str]:
        return sorted({it["mood"] for it in working if it.get("mood")})

    def _collect_tags(self, working: list[dict[str, Any]]) -> list[str]:
        s: set[str] = set()
        for it in working:
            for t in it.get("tags") or []:
                s.add(t)
        return sorted(s)

    def _find_item(self, working: list[dict[str, Any]], id_: Any) -> dict[str, Any] | None:
        try:
            target = int(id_)
        except (TypeError, ValueError):
            return None
        for it in working:
            raw = it.get("id")
            try:
                if int(raw) == target:
                    return it
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _fmt_line(item: dict[str, Any]) -> str:
        prefix = "[已删] " if item.get("_deleted") else ""
        preview = (item.get("content") or "").replace("\n", " ")[:28]
        return f"{prefix}#{item['id']} [{item.get('date','')}] {preview}"

    @staticmethod
    def _parse_date(text: str) -> str | None:
        try:
            datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            return text
        except ValueError:
            return None

    async def _fetch_to_preview_cache(self, url: str) -> Path | None:
        """下载远端图片到 image_cache_dir，按 URL 最后一段命名以便去重。"""
        import aiohttp

        name = url.rsplit("/", 1)[-1] or f"{uuid.uuid4().hex}.bin"
        local = self.image_cache_dir / f"preview_{name}"
        if local.exists() and local.stat().st_size > 0:
            return local
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status != 200:
                        logger.warning(f"[mizuki_diary] fetch {url} -> {r.status}")
                        return None
                    data = await r.read()
        except Exception as e:
            logger.warning(f"[mizuki_diary] fetch {url} error: {e}")
            return None
        local.write_bytes(data)
        return local

    async def _download_and_process_image(
        self, img_comp: Comp.Image, diary_date: str, diary_id: int, index: int
    ) -> dict[str, str]:
        data = await image_utils.extract_image_bytes(img_comp)
        webp = image_utils.to_webp(data, int(self.config.get("webp_quality", 85)))
        date_part = diary_date.split(" ")[0]  # YYYY-MM-DD
        filename = f"{date_part}-{diary_id}-{index}.webp"
        local = self.image_cache_dir / f"{uuid.uuid4().hex}.webp"
        local.write_bytes(webp)
        image_repo_dir = self.config.get("image_repo_dir", "images/diary").strip("/")
        url_prefix = self.config.get("image_url_prefix", "/images/diary").rstrip("/")
        return {
            "local_path": str(local),
            "remote_path": f"{image_repo_dir}/{filename}",
            "url": f"{url_prefix}/{filename}",
            "filename": filename,
        }

    # --------------------------------------------------------------- permission

    def _deny(self, event: AstrMessageEvent):
        return event.plain_result("无权使用本插件，请联系管理员将你的 QQ 加入白名单。")

    # --------------------------------------------------------------- commands

    @filter.command_group("diary")
    def diary(self):
        pass

    @diary.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        yield event.plain_result(HELP_TEXT)

    @diary.command("cancel")
    async def cmd_cancel(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        yield event.plain_result("当前未处于多轮对话中。若正在多轮对话，请直接发送 /diary cancel。")

    @diary.command("list")
    async def cmd_list(self, event: AstrMessageEvent, page: int = 1):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        try:
            _, working = await self._current_view()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"获取失败：{e}")
            return
        working.sort(key=lambda it: str(it.get("date", "")), reverse=True)
        page_size = int(self.config.get("list_page_size", 10))
        total = len(working)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        chunk = working[start : start + page_size]
        if not chunk:
            yield event.plain_result("暂无日记。")
            return
        lines = [self._fmt_line(it) for it in chunk]
        header = f"日记列表（第 {page}/{total_pages} 页，共 {total} 条）"
        if self.store.has_pending():
            header += f" · 含 {sum(self.store.summary().values())} 条未推送"
        yield event.plain_result(header + "\n" + "\n".join(lines))

    @diary.command("preview")
    async def cmd_preview(self, event: AstrMessageEvent, id: int):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        try:
            _, working = await self._current_view()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"获取失败：{e}")
            return
        item = self._find_item(working, id)
        if not item:
            yield event.plain_result(f"未找到 id={id} 的日记。")
            return
        yield event.plain_result(self._render_item_text(item))
        for url in item.get("images") or []:
            abs_url = self._resolve_image_url(url)
            if not abs_url:
                continue
            local_path = await self._fetch_to_preview_cache(abs_url)
            if local_path is None:
                yield event.plain_result(f"[图片加载失败] {abs_url}")
                continue
            yield event.chain_result([Comp.Image.fromFileSystem(str(local_path))])

    def _render_item_text(self, item: dict[str, Any]) -> str:
        lines = []
        tag = "[已删] " if item.get("_deleted") else ""
        lines.append(f"{tag}#{item.get('id')}  {item.get('date','')}")
        if item.get("location"):
            lines.append(f"地点：{item['location']}")
        if item.get("mood"):
            lines.append(f"心情：{item['mood']}")
        if item.get("tags"):
            lines.append("标签：" + ", ".join(item["tags"]))
        lines.append("")
        lines.append(item.get("content") or "")
        if item.get("images"):
            lines.append("")
            lines.append(f"（{len(item['images'])} 张图）")
        return "\n".join(lines)

    def _resolve_image_url(self, url: str) -> str | None:
        """把条目里存的 /images/diary/xxx.webp 转成 raw.githubusercontent 绝对 URL。"""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        repo = self.config.get("github_repo", "")
        branch = self.config.get("github_branch", "master")
        url_prefix = self.config.get("image_url_prefix", "/images/diary").rstrip("/")
        image_repo_dir = self.config.get("image_repo_dir", "images/diary").strip("/")
        if url.startswith(url_prefix + "/"):
            tail = url[len(url_prefix) + 1 :]
            return f"https://raw.githubusercontent.com/{repo}/{branch}/{image_repo_dir}/{tail}"
        return None

    @diary.command("del")
    async def cmd_del(self, event: AstrMessageEvent, id: int):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        try:
            _, working = await self._current_view()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"获取失败：{e}")
            return
        item = self._find_item(working, id)
        if not item:
            yield event.plain_result(f"未找到 id={id} 的日记。")
            return
        if item.get("_deleted"):
            yield event.plain_result(f"#{id} 已是删除状态。")
            return
        self.store.add_patch({"op": "delete", "id": int(item.get("id", id))})
        yield event.plain_result(f"已标记 #{id} 删除，/diary push 推送。")

    @diary.command("restore")
    async def cmd_restore(self, event: AstrMessageEvent, id: int):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        try:
            _, working = await self._current_view()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"获取失败：{e}")
            return
        item = self._find_item(working, id)
        if not item:
            yield event.plain_result(f"未找到 id={id}。")
            return
        if not item.get("_deleted"):
            yield event.plain_result(f"#{id} 当前未被删除，无需恢复。")
            return
        self.store.add_patch({"op": "restore", "id": int(item.get("id", id))})
        yield event.plain_result(f"已标记 #{id} 恢复，/diary push 推送。")

    @diary.command("diff")
    async def cmd_diff(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        if not self.store.has_pending():
            yield event.plain_result("当前无 pending 改动。")
            return
        lines = [f"pending 改动（{len(self.store.patches)} 条）："]
        for i, p in enumerate(self.store.patches, 1):
            op = p.get("op")
            if op == "add":
                it = p["item"]
                preview = (it.get("content") or "").replace("\n", " ")[:20]
                lines.append(f"{i}. +新增 #{it.get('id')} {preview}")
            elif op == "edit":
                keys = ",".join(p.get("fields", {}).keys())
                lines.append(f"{i}. ~修改 #{p['id']} 字段={keys}")
            elif op == "delete":
                lines.append(f"{i}. -删除 #{p['id']}")
            elif op == "restore":
                lines.append(f"{i}. ↺恢复 #{p['id']}")
            else:
                lines.append(f"{i}. ?未知操作 {p}")
        yield event.plain_result("\n".join(lines))

    @diary.command("discard")
    async def cmd_discard(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        if not self.store.has_pending():
            yield event.plain_result("当前无 pending 改动。")
            return
        self._cleanup_cached_images()
        self.store.clear()
        yield event.plain_result("已放弃所有 pending 改动。")

    def _cleanup_cached_images(self):
        for p in self.store.patches:
            for f in p.get("image_files", []) or []:
                try:
                    Path(f["local_path"]).unlink(missing_ok=True)
                except Exception:
                    pass

    # ------------------------------------------------------ quick (2-step)

    @staticmethod
    def _strip_quick_prefix(s: str) -> str:
        """去掉 '/diary quick' / 'diary quick' 前缀，返回剩余文本（保留内部换行）。

        AstrBot 不同版本可能：(a) 把完整消息 `/diary quick xxx` 原样放进 message_str；
        (b) 剥掉指令只留参数；(c) 剥到只剩 `quick xxx`。三种情况都兼容。
        """
        if not s:
            return ""
        stripped = s.lstrip()
        low = stripped.lower()
        for pref in ("/diary quick", "diary quick", "quick"):
            if low.startswith(pref):
                rest = stripped[len(pref):]
                if not rest or rest[0] in " \t\r\n":
                    return rest.lstrip(" \t\r\n")
        return s

    _QUICK_FIELD_ALIASES = {
        "地点": "location", "location": "location",
        "心情": "mood", "mood": "mood",
        "标签": "tags", "tags": "tags", "tag": "tags",
        "日期": "date", "date": "date",
    }

    def _apply_quick_text(self, item: dict[str, Any], text: str) -> None:
        """解析首部 '#key:value' 行为字段，剩下当正文。"""
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            if not line.startswith("#"):
                break
            header = line[1:].strip()
            if ":" in header:
                k, _, v = header.partition(":")
            elif "：" in header:
                k, _, v = header.partition("：")
            else:
                break
            canonical = self._QUICK_FIELD_ALIASES.get(k.strip().lower())
            if not canonical:
                break
            v = v.strip()
            if not v:
                i += 1
                continue
            if canonical == "tags":
                parts = [t.strip() for t in v.replace("，", ",").split(",") if t.strip()]
                if parts:
                    item["tags"] = parts
            elif canonical == "date":
                parsed = self._parse_date(v)
                if parsed:
                    item["date"] = parsed
            else:
                item[canonical] = v
            i += 1
        content_lines = lines[i:]
        while content_lines and not content_lines[-1].strip():
            content_lines.pop()
        while content_lines and not content_lines[0].strip():
            content_lines.pop(0)
        if content_lines:
            item["content"] = "\n".join(content_lines)
        if "date" not in item:
            item["date"] = self._now_str()

    @diary.command("quick")
    async def cmd_quick(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        try:
            _, working = await self._current_view()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"获取远程失败：{e}")
            return

        new_id = self._next_id(working)
        body = self._strip_quick_prefix(event.message_str or "")

        state: dict[str, Any] = {
            "step": "images" if body else "content",
            "item": {"id": new_id},
            "images": [],
            "cancelled": False,
            "completed": False,
            "error": None,
        }

        if body:
            self._apply_quick_text(state["item"], body)
            if not state["item"].get("content"):
                yield event.plain_result(
                    f"文字为空（只识别到 # 字段头）。请重发一条作为正文，{CANCEL_DISPLAY} 取消。"
                )
                state["step"] = "content"
            else:
                yield event.plain_result(
                    f"文字已记录 #{new_id}。现在发图片（可多张），"
                    f"完成发 {DONE_DISPLAY}，无图发 skip。"
                )
        else:
            yield event.plain_result(
                f"/diary quick #{new_id}：请发一条消息作为日记正文。\n"
                f"首行可加 #地点:xxx / #心情:xxx / #标签:a,b / #日期:YYYY-MM-DD HH:mm:ss。\n"
                f"{CANCEL_DISPLAY} 取消。"
            )

        timeout = self._timeout()

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            if not _event_has_payload(evt):
                controller.keep(timeout=timeout, reset_timeout=False)
                return
            text = (evt.message_str or "").strip()

            if _is_token(text, CANCEL_TOKENS):
                state["cancelled"] = True
                await evt.send(evt.plain_result("已取消。"))
                controller.stop()
                return

            try:
                if state["step"] == "content":
                    if _looks_like_slash_cmd(text):
                        await evt.send(evt.plain_result(
                            f"正文不能以 / 开头。{CANCEL_DISPLAY} 取消。"
                        ))
                        controller.keep(timeout=timeout, reset_timeout=True)
                        return
                    self._apply_quick_text(state["item"], evt.message_str or "")
                    if not state["item"].get("content"):
                        await evt.send(evt.plain_result(
                            "还是没有正文内容，请再发一条。"
                        ))
                        controller.keep(timeout=timeout, reset_timeout=True)
                        return
                    state["step"] = "images"
                    await evt.send(evt.plain_result(
                        f"文字已记录。现在发图片，完成发 {DONE_DISPLAY}，无图发 skip。"
                    ))
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return

                imgs = [seg for seg in evt.get_messages() if isinstance(seg, Comp.Image)]
                if imgs:
                    for seg in imgs:
                        index = len(state["images"]) + 1
                        info = await self._download_and_process_image(
                            seg, state["item"]["date"], new_id, index
                        )
                        state["images"].append(info)
                    await evt.send(evt.plain_result(
                        f"已收到 {len(state['images'])} 张图，继续或 {DONE_DISPLAY} 完成。"
                    ))
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return

                if _is_token(text, DONE_TOKENS) or _is_token(text, SKIP_TOKENS):
                    if state["images"]:
                        state["item"]["images"] = [i["url"] for i in state["images"]]
                    state["completed"] = True
                    controller.stop()
                    return

                await evt.send(evt.plain_result(
                    f"请发图片，或 {DONE_DISPLAY} 完成 / skip 无图完成。"
                ))
                controller.keep(timeout=timeout, reset_timeout=True)
            except ImageError as e:
                await evt.send(evt.plain_result(f"图片处理失败：{e}"))
                controller.keep(timeout=timeout, reset_timeout=True)
            except Exception as e:
                logger.exception("diary quick error")
                state["error"] = str(e)
                controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            event.stop_event()
            for img in state["images"]:
                Path(img["local_path"]).unlink(missing_ok=True)
            yield event.plain_result("会话超时，已取消。")
            return

        event.stop_event()

        if state["cancelled"] or state["error"]:
            for img in state["images"]:
                Path(img["local_path"]).unlink(missing_ok=True)
            if state["error"]:
                yield event.plain_result(f"出错：{state['error']}")
            return
        if not state["completed"]:
            return

        item = state["item"]
        item["_deleted"] = False
        image_files = [
            {"local_path": i["local_path"], "remote_path": i["remote_path"]}
            for i in state["images"]
        ]
        self.store.add_patch({"op": "add", "item": item, "image_files": image_files})
        yield event.plain_result(
            f"已暂存新增 #{new_id}。/diary push 推送，/diary preview {new_id} 预览。"
        )

    # -------------------------------------------------------- add (multi-turn)

    @diary.command("add")
    async def cmd_add(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield self._deny(event)
            return

        try:
            _, working = await self._current_view()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"获取远程失败：{e}")
            return

        new_id = self._next_id(working)
        moods_hist = self._collect_moods(working)
        tags_hist = self._collect_tags(working)

        state: dict[str, Any] = {
            "step": "content",
            "content_lines": [],
            "images": [],
            "item": {"id": new_id},
            "moods_hist": moods_hist,
            "tags_hist": tags_hist,
            "cancelled": False,
            "completed": False,
            "error": None,
        }

        yield event.plain_result(
            f"开始新建日记 #{new_id}。\n"
            f"请发送内容（支持多条消息），完成后发 {DONE_DISPLAY}。\n"
            f"内容不能以 / 开头。随时可发 {CANCEL_DISPLAY} 取消。"
        )

        timeout = self._timeout()

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            if not _event_has_payload(evt):
                controller.keep(timeout=timeout, reset_timeout=False)
                return
            text = evt.message_str.strip() if evt.message_str else ""

            if _is_token(text, CANCEL_TOKENS):
                state["cancelled"] = True
                await evt.send(evt.plain_result("已取消，pending 未保存。"))
                controller.stop()
                return

            step = state["step"]
            try:
                if step == "content":
                    await self._step_content(controller, evt, state, timeout)
                elif step == "date":
                    await self._step_date(controller, evt, state, timeout)
                elif step == "images":
                    await self._step_images(controller, evt, state, timeout, new_id)
                elif step == "location":
                    await self._step_location(controller, evt, state, timeout)
                elif step == "mood":
                    await self._step_mood(controller, evt, state, timeout, moods_hist)
                elif step == "tags":
                    await self._step_tags(controller, evt, state, timeout, tags_hist)
            except ImageError as e:
                await evt.send(evt.plain_result(f"图片处理失败：{e}"))
                controller.keep(timeout=timeout, reset_timeout=True)
            except Exception as e:
                logger.exception("diary add step error")
                state["error"] = str(e)
                controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            event.stop_event()
            # 清理已缓存的本次图片
            for img in state.get("images", []):
                Path(img["local_path"]).unlink(missing_ok=True)
            yield event.plain_result("会话超时，已取消本次新增。")
            return

        event.stop_event()

        if state.get("cancelled"):
            for img in state.get("images", []):
                Path(img["local_path"]).unlink(missing_ok=True)
            return
        if state.get("error"):
            yield event.plain_result(f"出错：{state['error']}")
            return
        if not state.get("completed"):
            return

        item = state["item"]
        item["_deleted"] = False
        image_files = [
            {"local_path": i["local_path"], "remote_path": i["remote_path"]}
            for i in state["images"]
        ]
        self.store.add_patch({"op": "add", "item": item, "image_files": image_files})
        yield event.plain_result(
            f"已暂存新增 #{new_id}。使用 /diary push 推送，/diary preview {new_id} 预览。"
        )

    async def _step_content(self, controller, evt, state, timeout):
        text_raw = evt.message_str or ""
        text = text_raw.strip()

        if _is_token(text, DONE_TOKENS):
            if not state["content_lines"]:
                await evt.send(evt.plain_result(
                    f"尚未输入内容，请先发送，或 {CANCEL_DISPLAY} 取消。"))
            else:
                state["item"]["content"] = "\n".join(state["content_lines"])
                state["step"] = "date"
                await evt.send(evt.plain_result(
                    f"内容已记录。\n现在请发日期（YYYY-MM-DD HH:mm:ss），"
                    f"或发 skip 使用当前时间 {self._now_str()}。"
                ))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        if _looks_like_slash_cmd(text):
            await evt.send(evt.plain_result(
                f"content 不能以 / 开头（避免与指令冲突）。\n"
                f"完成输入发 {DONE_DISPLAY}，取消发 {CANCEL_DISPLAY}。"
            ))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        state["content_lines"].append(text_raw)
        await evt.send(evt.plain_result(
            f"已记录第 {len(state['content_lines'])} 段，"
            f"继续发送或 {DONE_DISPLAY} 完成。"
        ))
        controller.keep(timeout=timeout, reset_timeout=True)

    async def _step_date(self, controller, evt, state, timeout):
        text = (evt.message_str or "").strip()
        if _is_token(text, SKIP_TOKENS):
            state["item"]["date"] = self._now_str()
        else:
            parsed = self._parse_date(text)
            if parsed is None:
                await evt.send(evt.plain_result(
                    "日期格式错误，请用 YYYY-MM-DD HH:mm:ss 或发 skip。"))
                controller.keep(timeout=timeout, reset_timeout=True)
                return
            state["item"]["date"] = parsed
        state["step"] = "images"
        await evt.send(evt.plain_result(
            f"日期：{state['item']['date']}。\n"
            f"现在请发图片（可多张，自动转 WebP），完成发 {DONE_DISPLAY}，跳过发 skip。"
            "\n注意：不支持 GIF。"
        ))
        controller.keep(timeout=timeout, reset_timeout=True)

    async def _step_images(self, controller, evt, state, timeout, diary_id):
        text = (evt.message_str or "").strip()
        imgs = [seg for seg in evt.get_messages() if isinstance(seg, Comp.Image)]

        if imgs:
            for seg in imgs:
                index = len(state["images"]) + 1
                info = await self._download_and_process_image(
                    seg, state["item"]["date"], diary_id, index
                )
                state["images"].append(info)
            await evt.send(evt.plain_result(
                f"已收到 {len(state['images'])} 张图，继续发送或 {DONE_DISPLAY} 进入下一步。\n"
                f"若要丢弃已收到的图片请发 clear。"
            ))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        if _is_token(text, DONE_TOKENS) or (
            _is_token(text, SKIP_TOKENS) and state["images"]
        ):
            if state["images"]:
                state["item"]["images"] = [i["url"] for i in state["images"]]
            state["step"] = "location"
            await evt.send(evt.plain_result(
                f"已记录 {len(state['images'])} 张图。现在请发地点，skip 跳过。"
            ))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        if _is_token(text, SKIP_TOKENS):
            state["step"] = "location"
            await evt.send(evt.plain_result("已跳过图片。现在请发地点，skip 跳过。"))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        if _is_token(text, CLEAR_TOKENS):
            for i in state["images"]:
                Path(i["local_path"]).unlink(missing_ok=True)
            state["images"] = []
            await evt.send(evt.plain_result(
                f"已清空。继续发图片，或 {DONE_DISPLAY} / skip 进入下一步。"
            ))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        await evt.send(evt.plain_result(f"请发图片，或 {DONE_DISPLAY} 完成 / skip 跳过。"))
        controller.keep(timeout=timeout, reset_timeout=True)

    async def _step_location(self, controller, evt, state, timeout):
        text = (evt.message_str or "").strip()
        if _is_token(text, SKIP_TOKENS):
            pass
        elif text:
            state["item"]["location"] = text
        state["step"] = "mood"
        moods_hist = state.get("moods_hist") or []
        hint = ("  历史：" + ", ".join(moods_hist[:20])) if moods_hist else ""
        await evt.send(evt.plain_result("现在请发心情（自由文本），skip 跳过。" + hint))
        controller.keep(timeout=timeout, reset_timeout=True)

    async def _step_mood(self, controller, evt, state, timeout, moods_hist):
        text = (evt.message_str or "").strip()
        if _is_token(text, SKIP_TOKENS):
            pass
        elif text:
            state["item"]["mood"] = text
        state["step"] = "tags"
        tags_hist = state.get("tags_hist") or []
        hint = ("  历史：" + ", ".join(tags_hist[:20])) if tags_hist else ""
        await evt.send(evt.plain_result(
            "现在请发标签（逗号分隔，如 工作,生活），skip 跳过。" + hint
        ))
        controller.keep(timeout=timeout, reset_timeout=True)

    async def _step_tags(self, controller, evt, state, timeout, tags_hist):
        text = (evt.message_str or "").strip()
        if not _is_token(text, SKIP_TOKENS) and text:
            parts = [t.strip() for t in text.replace("，", ",").split(",") if t.strip()]
            if parts:
                state["item"]["tags"] = parts
        state["completed"] = True
        controller.stop()

    # ------------------------------------------------------- edit (multi-turn)

    @diary.command("edit")
    async def cmd_edit(self, event: AstrMessageEvent, id: int):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        try:
            _, working = await self._current_view()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"获取远程失败：{e}")
            return
        item = self._find_item(working, id)
        if not item:
            yield event.plain_result(f"未找到 id={id}。")
            return

        moods_hist = self._collect_moods(working)

        state: dict[str, Any] = {
            "phase": "pick_field",
            "field": None,
            "content_lines": [],
            "images": [],
            "patch_fields": {},
            "cancelled": False,
            "completed": False,
            "error": None,
        }

        yield event.plain_result(
            f"修改 #{id}：\n"
            f"当前内容：{(item.get('content') or '')[:50]}\n"
            "请选择要修改的字段：\n"
            "1. content  2. date  3. images  4. location  5. mood  6. tags\n"
            f"回复编号；或 {CANCEL_DISPLAY} 取消。"
        )

        timeout = self._timeout()

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            if not _event_has_payload(evt):
                controller.keep(timeout=timeout, reset_timeout=False)
                return
            text = (evt.message_str or "").strip()

            if _is_token(text, CANCEL_TOKENS):
                state["cancelled"] = True
                await evt.send(evt.plain_result("已取消。"))
                controller.stop()
                return

            try:
                if state["phase"] == "pick_field":
                    await self._edit_pick(controller, evt, state, item, moods_hist, timeout)
                else:
                    await self._edit_input(controller, evt, state, item, id, timeout)
            except ImageError as e:
                await evt.send(evt.plain_result(f"图片处理失败：{e}"))
                controller.keep(timeout=timeout, reset_timeout=True)
            except Exception as e:
                logger.exception("diary edit error")
                state["error"] = str(e)
                controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            event.stop_event()
            for img in state.get("images", []):
                Path(img["local_path"]).unlink(missing_ok=True)
            yield event.plain_result("会话超时。")
            return

        event.stop_event()
        if state.get("cancelled") or state.get("error"):
            for img in state.get("images", []):
                Path(img["local_path"]).unlink(missing_ok=True)
            if state.get("error"):
                yield event.plain_result(f"出错：{state['error']}")
            return
        if not state.get("completed"):
            return

        image_files = [
            {"local_path": i["local_path"], "remote_path": i["remote_path"]}
            for i in state["images"]
        ]
        patch = {"op": "edit", "id": int(item.get("id", id)), "fields": state["patch_fields"]}
        if image_files:
            patch["image_files"] = image_files
        self.store.add_patch(patch)
        yield event.plain_result(
            f"已暂存对 #{id} 的修改（字段：{','.join(state['patch_fields'].keys())}）。"
        )

    async def _edit_pick(self, controller, evt, state, item, moods_hist, timeout):
        text = (evt.message_str or "").strip()
        mapping = {
            "1": "content", "2": "date", "3": "images",
            "4": "location", "5": "mood", "6": "tags",
        }
        field = mapping.get(text)
        if not field:
            await evt.send(evt.plain_result("请回复 1-6 中的数字。"))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        state["field"] = field
        state["phase"] = "input"

        if field == "content":
            cur = (item.get("content") or "")
            await evt.send(evt.plain_result(
                f"当前：\n{cur}\n\n请发送新内容（可多条消息，不能以 / 开头），"
                f"完成发 {DONE_DISPLAY}；发 skip 保持不变。"
            ))
        elif field == "date":
            await evt.send(evt.plain_result(
                f"当前：{item.get('date','')}\n请发送新日期 YYYY-MM-DD HH:mm:ss，skip 保持。"
            ))
        elif field == "images":
            cur = item.get("images") or []
            await evt.send(evt.plain_result(
                f"当前有 {len(cur)} 张图。\n"
                f"请发送新图片序列（将完全替换原有）。\n"
                f"{DONE_DISPLAY} 完成；clear 清空为空；skip 保持不变。"
            ))
        elif field == "location":
            await evt.send(evt.plain_result(
                f"当前：{item.get('location','')}\n请发新地点，skip 保持，clear 清空。"
            ))
        elif field == "mood":
            hint = ("（历史：" + ", ".join(moods_hist[:20]) + "）") if moods_hist else ""
            await evt.send(evt.plain_result(
                f"当前：{item.get('mood','')}\n请发新心情，skip 保持，clear 清空。{hint}"
            ))
        elif field == "tags":
            cur = ", ".join(item.get("tags") or [])
            await evt.send(evt.plain_result(
                f"当前：{cur}\n请发新标签（逗号分隔），skip 保持，clear 清空。"
            ))
        controller.keep(timeout=timeout, reset_timeout=True)

    async def _edit_input(self, controller, evt, state, item, diary_id, timeout):
        field = state["field"]
        text_raw = evt.message_str or ""
        text = text_raw.strip()

        if field == "content":
            if _is_token(text, DONE_TOKENS):
                if not state["content_lines"]:
                    await evt.send(evt.plain_result("内容为空，继续输入或 skip 保持原样。"))
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return
                state["patch_fields"]["content"] = "\n".join(state["content_lines"])
                state["completed"] = True
                controller.stop()
                return
            if _is_token(text, SKIP_TOKENS):
                state["completed"] = True
                controller.stop()
                return
            if _looks_like_slash_cmd(text):
                await evt.send(evt.plain_result(
                    f"content 不能以 / 开头。完成发 {DONE_DISPLAY}，保持不变发 skip。"))
                controller.keep(timeout=timeout, reset_timeout=True)
                return
            state["content_lines"].append(text_raw)
            await evt.send(evt.plain_result(
                f"已记录第 {len(state['content_lines'])} 段，继续或 {DONE_DISPLAY}。"))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        if field == "date":
            if _is_token(text, SKIP_TOKENS):
                state["completed"] = True
                controller.stop()
                return
            parsed = self._parse_date(text)
            if parsed is None:
                await evt.send(evt.plain_result("格式错误，请重试或 skip。"))
                controller.keep(timeout=timeout, reset_timeout=True)
                return
            state["patch_fields"]["date"] = parsed
            state["completed"] = True
            controller.stop()
            return

        if field == "images":
            imgs = [seg for seg in evt.get_messages() if isinstance(seg, Comp.Image)]
            if imgs:
                date = state["patch_fields"].get("date") or item.get("date") or self._now_str()
                for seg in imgs:
                    index = len(state["images"]) + 1
                    info = await self._download_and_process_image(seg, date, diary_id, index)
                    state["images"].append(info)
                await evt.send(evt.plain_result(
                    f"已收到 {len(state['images'])} 张，继续或 {DONE_DISPLAY} 完成。"))
                controller.keep(timeout=timeout, reset_timeout=True)
                return
            if _is_token(text, DONE_TOKENS):
                state["patch_fields"]["images"] = [i["url"] for i in state["images"]]
                state["completed"] = True
                controller.stop()
                return
            if _is_token(text, CLEAR_TOKENS):
                for i in state["images"]:
                    Path(i["local_path"]).unlink(missing_ok=True)
                state["images"] = []
                state["patch_fields"]["images"] = []
                state["completed"] = True
                controller.stop()
                return
            if _is_token(text, SKIP_TOKENS):
                for i in state["images"]:
                    Path(i["local_path"]).unlink(missing_ok=True)
                state["images"] = []
                state["completed"] = True
                controller.stop()
                return
            await evt.send(evt.plain_result(f"发图片 / {DONE_DISPLAY} / skip / clear。"))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        if field in ("location", "mood"):
            if _is_token(text, SKIP_TOKENS):
                state["completed"] = True
                controller.stop()
                return
            if _is_token(text, CLEAR_TOKENS):
                state["patch_fields"][field] = ""
                state["completed"] = True
                controller.stop()
                return
            if text:
                state["patch_fields"][field] = text
                state["completed"] = True
                controller.stop()
                return
            await evt.send(evt.plain_result("请输入内容或 skip/clear。"))
            controller.keep(timeout=timeout, reset_timeout=True)
            return

        if field == "tags":
            if _is_token(text, SKIP_TOKENS):
                state["completed"] = True
                controller.stop()
                return
            if _is_token(text, CLEAR_TOKENS):
                state["patch_fields"]["tags"] = []
                state["completed"] = True
                controller.stop()
                return
            parts = [t.strip() for t in text.replace("，", ",").split(",") if t.strip()]
            if parts:
                state["patch_fields"]["tags"] = parts
            state["completed"] = True
            controller.stop()
            return

    # ------------------------------------------------------------------- push

    @diary.command("push")
    async def cmd_push(self, event: AstrMessageEvent):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        if not self.store.has_pending():
            yield event.plain_result("无 pending 改动，无需推送。")
            return
        counts = self.store.summary()
        summary = f"+{counts['add']} 新增 · ~{counts['edit']} 修改 · -{counts['delete']} 删除 · ↺{counts['restore']} 恢复"
        yield event.plain_result(
            f"即将推送到 {self.config.get('github_repo')}@{self.config.get('github_branch')}：\n"
            f"{summary}\n\n回复 确认 继续，其他任意回复取消。"
        )

        timeout = self._timeout()
        state = {"confirmed": False, "cancelled": False}

        @session_waiter(timeout=60, record_history_chains=False)
        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            if not _event_has_payload(evt):
                controller.keep(timeout=60, reset_timeout=False)
                return
            text = (evt.message_str or "").strip()
            if text == "确认":
                state["confirmed"] = True
            else:
                state["cancelled"] = True
            controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            event.stop_event()
            yield event.plain_result("确认超时，已取消推送。")
            return

        event.stop_event()

        if not state["confirmed"]:
            yield event.plain_result("已取消推送。")
            return

        try:
            result = await self._do_push()
        except Exception as e:
            logger.exception("diary push failed")
            yield event.plain_result(f"推送异常：{e}")
            return

        yield event.plain_result(self._format_push_result(result))

    # ------------------------------------------------------------------- sync

    @diary.command("sync")
    async def cmd_sync(self, event: AstrMessageEvent, target: str = ""):
        if not self._is_allowed(event):
            yield self._deny(event)
            return
        t = (target or "").strip().lower()
        if t == "memos":
            async for msg in self._sync_to_memos(event):
                yield msg
        elif t == "mizuki":
            async for msg in self._sync_from_memos(event):
                yield msg
        else:
            yield event.plain_result(
                "用法：\n"
                "/diary sync memos   GitHub → memos（补齐未映射的条目）\n"
                "/diary sync mizuki  memos → GitHub（生成 pending，再 /diary push）"
            )

    async def _sync_to_memos(self, event: AstrMessageEvent):
        memos = self._memos_client()
        if memos is None:
            yield event.plain_result("memos 未启用，请先在插件配置开启 memos_enabled。")
            return
        try:
            _, working = await self._current_view()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"获取远程失败：{e}")
            return

        ok = failed = skipped_mapped = skipped_no_id = 0
        img_missing = 0
        errors: list[str] = []

        for item in working:
            diary_id = item.get("id")
            if diary_id is None:
                skipped_no_id += 1
                continue
            if self.memos_map.get(diary_id):
                skipped_mapped += 1
                continue
            try:
                attachments, miss = await self._upload_item_images_to_memos(
                    memos, item
                )
                img_missing += miss
                content = self._format_memo_content(item)
                visibility = "PRIVATE" if item.get("_deleted") else None
                memo_name = await memos.create_memo(
                    content, attachments=attachments, visibility=visibility
                )
                self.memos_map.set(diary_id, memo_name)
                ok += 1
            except MemosError as e:
                failed += 1
                errors.append(f"#{diary_id}: {e}")
            except Exception as e:
                logger.exception("sync to memos failed for item")
                failed += 1
                errors.append(f"#{diary_id}: {e}")

        lines = [
            f"GitHub → memos 完成：",
            f"  新建 {ok}、跳过已映射 {skipped_mapped}、失败 {failed}",
        ]
        if img_missing:
            lines.append(f"  其中 {img_missing} 张图片下载失败（memo 已建、无附件）")
        for err in errors[:3]:
            lines.append(f"  · {err}")
        yield event.plain_result("\n".join(lines))

    async def _upload_item_images_to_memos(
        self, memos: MemosClient, item: dict[str, Any]
    ) -> tuple[list[str], int]:
        """把 item.images（GitHub 路径）下载 → 上传到 memos，返回 (attachments, 失败数)。"""
        import aiohttp

        attachments: list[str] = []
        failed = 0
        for url in item.get("images") or []:
            abs_url = self._resolve_image_url(url)
            if not abs_url:
                failed += 1
                continue
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        abs_url, timeout=aiohttp.ClientTimeout(total=30)
                    ) as r:
                        if r.status != 200:
                            failed += 1
                            continue
                        data = await r.read()
                filename = abs_url.rsplit("/", 1)[-1] or "image.webp"
                name = await memos.create_attachment(
                    filename, data, mime="image/webp"
                )
                attachments.append(name)
            except Exception as e:
                logger.warning(f"[mizuki_diary] upload {abs_url} -> memos failed: {e}")
                failed += 1
        return attachments, failed

    async def _sync_from_memos(self, event: AstrMessageEvent):
        memos = self._memos_client()
        if memos is None:
            yield event.plain_result("memos 未启用，请先在插件配置开启 memos_enabled。")
            return
        try:
            _, working = await self._current_view()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"获取远程失败：{e}")
            return

        try:
            all_memos = await memos.list_memos()
        except MemosError as e:
            yield event.plain_result(f"获取 memos 列表失败：{e}")
            return

        reverse_map = self.memos_map.reverse()
        added = edited = skipped = 0
        img_failed = 0
        errors: list[str] = []
        next_id = self._next_id(working)

        for memo in all_memos:
            content = memo.get("content") or ""
            first_line = content.splitlines()[0].strip() if content else ""
            if not first_line.startswith("#日记"):
                skipped += 1
                continue
            if (memo.get("state") or "NORMAL").upper() == "ARCHIVED":
                skipped += 1
                continue

            memo_name = memo.get("name") or ""
            diary_id_str = reverse_map.get(memo_name)
            try:
                fields = self._parse_memo_content(content)
                img_files, miss = await self._fetch_memo_attachments(
                    memos, memo, fields.get("date") or self._now_str(),
                    int(diary_id_str) if diary_id_str else next_id,
                )
                img_failed += miss

                if diary_id_str is not None:
                    # 已映射 → 看 updateTime
                    diary_id = int(diary_id_str)
                    item = self._find_item(working, diary_id)
                    memo_update = _parse_iso8601(memo.get("updateTime") or "")
                    item_date = _parse_diary_date(
                        item.get("date") if item else None, self._tz()
                    )
                    if item and memo_update and item_date and memo_update <= item_date:
                        skipped += 1
                        continue
                    patch_fields = dict(fields)
                    if img_files:
                        patch_fields["images"] = [
                            f"{self.config.get('image_url_prefix', '/images/diary').rstrip('/')}/"
                            f"{Path(x['remote_path']).name}"
                            for x in img_files
                        ]
                    patch: dict[str, Any] = {
                        "op": "edit",
                        "id": diary_id,
                        "fields": patch_fields,
                        "_from_memos_sync": True,
                    }
                    if img_files:
                        patch["image_files"] = img_files
                    self.store.add_patch(patch)
                    edited += 1
                else:
                    # 未映射 → 新建
                    new_id = next_id
                    next_id += 1
                    date_str = fields.get("date") or _memo_display_date(
                        memo, self._tz()
                    ) or self._now_str()
                    new_item: dict[str, Any] = {
                        "id": new_id,
                        "content": fields.get("content", ""),
                        "date": date_str,
                    }
                    if fields.get("location"):
                        new_item["location"] = fields["location"]
                    if fields.get("mood"):
                        new_item["mood"] = fields["mood"]
                    if fields.get("tags"):
                        new_item["tags"] = fields["tags"]
                    if img_files:
                        url_prefix = self.config.get(
                            "image_url_prefix", "/images/diary"
                        ).rstrip("/")
                        new_item["images"] = [
                            f"{url_prefix}/{Path(x['remote_path']).name}"
                            for x in img_files
                        ]
                    patch = {
                        "op": "add",
                        "item": new_item,
                        "image_files": img_files,
                        "_from_memos_sync": True,
                        "_memo_name": memo_name,
                    }
                    self.store.add_patch(patch)
                    self.memos_map.set(new_id, memo_name)
                    added += 1
            except Exception as e:
                logger.exception("sync from memos failed for memo %s", memo.get("name"))
                errors.append(f"{memo.get('name')}: {e}")

        lines = [
            f"memos → pending 完成：",
            f"  新增 {added}、修改 {edited}、跳过 {skipped}、失败 {len(errors)}",
        ]
        if img_failed:
            lines.append(f"  {img_failed} 张附件下载/转换失败")
        for err in errors[:3]:
            lines.append(f"  · {err}")
        lines.append("使用 /diary diff 预览，/diary push 推送到 GitHub。")
        yield event.plain_result("\n".join(lines))

    _MEMO_META_RE = None  # filled below as module-level

    def _parse_memo_content(self, content: str) -> dict[str, Any]:
        """反解 memos 正文：去掉首行 #日记 ，识别 📍/💭 首段和末段 #tags，其余为正文。

        无法完全匹配格式时，除首行 #日记 外的全文作为 content。
        """
        lines = content.splitlines()
        if lines and lines[0].strip().startswith("#日记"):
            lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        body = "\n".join(lines)
        if not body:
            return {"content": ""}

        paragraphs = [p for p in body.split("\n\n")]
        fields: dict[str, Any] = {}

        if paragraphs:
            head = paragraphs[0].strip()
            loc, mood, matched = _parse_meta_line(head)
            if matched:
                if loc:
                    fields["location"] = loc
                if mood:
                    fields["mood"] = mood
                paragraphs = paragraphs[1:]

        if paragraphs:
            tail = paragraphs[-1].strip()
            tags = _parse_tag_line(tail)
            if tags is not None:
                clean_tags = [t for t in tags if t != "日记"]
                if clean_tags:
                    fields["tags"] = clean_tags
                paragraphs = paragraphs[:-1]

        fields["content"] = "\n\n".join(p.strip() for p in paragraphs).strip()
        return fields

    async def _fetch_memo_attachments(
        self,
        memos: MemosClient,
        memo: dict[str, Any],
        diary_date: str,
        diary_id: int,
    ) -> tuple[list[dict[str, str]], int]:
        """下载 memo 的附件 → 转 webp → 写入本地缓存，返回 image_files + 失败数。"""
        out: list[dict[str, str]] = []
        failed = 0
        image_repo_dir = self.config.get("image_repo_dir", "images/diary").strip("/")
        quality = int(self.config.get("webp_quality", 85))
        date_part = (diary_date or "").split(" ")[0] or "unknown"
        for att in memo.get("attachments") or []:
            att_name = att.get("name") or ""
            att_filename = att.get("filename") or "attachment"
            att_type = (att.get("type") or "").lower()
            if not att_name or not att_type.startswith("image/"):
                continue
            try:
                data = await memos.download_attachment(att_name, att_filename)
                webp = image_utils.to_webp(data, quality)
            except Exception as e:
                logger.warning(f"[mizuki_diary] fetch/convert {att_name} failed: {e}")
                failed += 1
                continue
            index = len(out) + 1
            filename = f"{date_part}-{diary_id}-{index}.webp"
            local = self.image_cache_dir / f"{uuid.uuid4().hex}.webp"
            local.write_bytes(webp)
            out.append(
                {
                    "local_path": str(local),
                    "remote_path": f"{image_repo_dir}/{filename}",
                }
            )
        return out, failed

    @staticmethod
    def _format_push_result(result: dict[str, Any]) -> str:
        lines: list[str] = []
        if result.get("github_ok"):
            sha = (result.get("commit_sha") or "")[:10]
            lines.append(f"GitHub 推送成功 ✅ commit: {sha}")
        elif result.get("github_error"):
            lines.append(f"GitHub 推送失败 ❌ {result['github_error']}")
        if result.get("memos_enabled"):
            ok = result.get("memos_ok", 0)
            fail = result.get("memos_failed", 0)
            skipped = result.get("memos_skipped", 0)
            parts = [f"memos 同步 {ok} 成功"]
            if fail:
                parts.append(f"{fail} 失败")
            if skipped:
                parts.append(f"{skipped} 跳过")
            emoji = "✅" if fail == 0 else "⚠️"
            lines.append(f"{emoji} " + "、".join(parts))
            if result.get("memos_errors"):
                for err in result["memos_errors"][:3]:
                    lines.append(f"  · {err}")
        return "\n".join(lines) if lines else "无操作"

    def _build_commit_message(self, working: list[dict[str, Any]]) -> str:
        """按 patch 顺序生成提交信息，例如 `diary: 新增日记（id: 4）# 2026-04-16 19:03:34`。"""
        parts: list[str] = []
        for p in self.store.patches:
            op = p.get("op")
            if op == "add":
                item = p.get("item") or {}
                pid = item.get("id", "?")
                date = item.get("date", "")
                tail = f"# {date}" if date else ""
                parts.append(f"新增日记（id: {pid}）{tail}".rstrip())
            elif op == "edit":
                pid = p.get("id", "?")
                fields = p.get("fields") or {}
                date = fields.get("date") or ""
                if not date:
                    try:
                        it = self._find_item(working, int(pid))
                        date = (it.get("date") if it else "") or ""
                    except (TypeError, ValueError):
                        pass
                tail = f"# {date}" if date else ""
                parts.append(f"修改日记（id: {pid}）{tail}".rstrip())
            elif op == "delete":
                parts.append(f"删除日记（id: {p.get('id', '?')}）")
            elif op == "restore":
                parts.append(f"恢复日记（id: {p.get('id', '?')}）")
        if not parts:
            return "diary: update"
        return "diary: " + "、".join(parts)

    async def _do_push(self) -> dict[str, Any]:
        """推送 pending → GitHub + memos（相互独立）。

        GitHub 失败不阻止 memos 同步；memos 失败也不影响 GitHub 的 commit。
        只有当 GitHub 和 memos 都失败（或 memos 禁用时 GitHub 失败）时保留 pending，
        其他情况清空 pending 并清理本地图片缓存。
        """
        patches_snapshot = list(self.store.patches)
        diary_path = self.config.get("diary_file_path", "data/diary.ts")

        client = self._client()
        result: dict[str, Any] = {
            "github_ok": False,
            "github_error": None,
            "commit_sha": None,
            "memos_enabled": bool(self.config.get("memos_enabled", False)),
            "memos_ok": 0,
            "memos_failed": 0,
            "memos_skipped": 0,
            "memos_errors": [],
        }

        # --- GitHub 推送 ---
        working: list[dict[str, Any]] = []
        try:
            content, _ = await client.get_file(diary_path)
            remote = diary_parser.parse(content)
            working = apply_patches(remote, patches_snapshot)
            new_content = diary_parser.regenerate(content, working)

            files: list[dict[str, Any]] = [
                {"path": diary_path, "content": new_content.encode("utf-8")}
            ]
            for img in self.store.collect_image_files():
                data = Path(img["local_path"]).read_bytes()
                files.append({"path": img["remote_path"], "content": data})

            message = self._build_commit_message(working)
            commit_sha = await client.commit_files(files, message)
            result["github_ok"] = True
            result["commit_sha"] = commit_sha
        except (GithubError, diary_parser.DiaryParseError) as e:
            result["github_error"] = str(e)
        except Exception as e:
            logger.exception("diary GitHub push failed")
            result["github_error"] = str(e)

        # --- memos 同步（独立于 GitHub）---
        if result["memos_enabled"]:
            # 若 GitHub 失败我们没有 working 视图，需要自建一个用于查 edit 后的完整条目
            if not working:
                try:
                    content, _ = await client.get_file(diary_path)
                    remote = diary_parser.parse(content)
                    working = apply_patches(remote, patches_snapshot)
                except Exception as e:
                    working = []
                    result["memos_errors"].append(f"取远程失败（仅用于 memos）：{e}")

            await self._sync_patches_to_memos(patches_snapshot, working, result)

        # --- 清理：GitHub 成功即可丢掉本地图片 & pending；失败则保留以便重试 ---
        if result["github_ok"]:
            for img in self.store.collect_image_files():
                try:
                    Path(img["local_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            self.store.clear()

        return result

    async def _sync_patches_to_memos(
        self,
        patches: list[dict[str, Any]],
        working: list[dict[str, Any]],
        result: dict[str, Any],
    ) -> None:
        """把 add/edit patches 同步到 memos；delete/restore 静默跳过。"""
        try:
            memos = self._memos_client()
        except MemosError as e:
            result["memos_errors"].append(f"memos 配置错误：{e}")
            result["memos_failed"] += sum(
                1 for p in patches if p.get("op") in ("add", "edit")
            )
            return
        if memos is None:
            return

        for p in patches:
            op = p.get("op")
            if op not in ("add", "edit"):
                result["memos_skipped"] += 1
                continue
            if p.get("_from_memos_sync"):
                # 这条 patch 本身就是从 memos 同步过来的，避免回环。
                result["memos_skipped"] += 1
                continue
            try:
                if op == "add":
                    await self._memos_sync_add(memos, p)
                else:
                    await self._memos_sync_edit(memos, p, working)
                result["memos_ok"] += 1
            except MemosError as e:
                result["memos_failed"] += 1
                pid = (p.get("item", {}).get("id") if op == "add" else p.get("id"))
                result["memos_errors"].append(f"#{pid} {op}: {e}")
            except Exception as e:
                logger.exception("memos sync failed for patch")
                result["memos_failed"] += 1
                pid = (p.get("item", {}).get("id") if op == "add" else p.get("id"))
                result["memos_errors"].append(f"#{pid} {op}: {e}")

    async def _memos_sync_add(self, memos: MemosClient, patch: dict[str, Any]) -> None:
        item = patch.get("item") or {}
        diary_id = item.get("id")
        attachments: list[str] = []
        for f in patch.get("image_files", []) or []:
            local_path = Path(f["local_path"])
            if not local_path.exists():
                continue
            data = local_path.read_bytes()
            name = await memos.create_attachment(
                local_path.name, data, mime="image/webp"
            )
            attachments.append(name)
        content = self._format_memo_content(item)
        memo_name = await memos.create_memo(content, attachments=attachments)
        if diary_id is not None:
            self.memos_map.set(diary_id, memo_name)

    async def _memos_sync_edit(
        self,
        memos: MemosClient,
        patch: dict[str, Any],
        working: list[dict[str, Any]],
    ) -> None:
        diary_id = patch.get("id")
        memo_name = self.memos_map.get(diary_id) if diary_id is not None else None
        full_item = self._find_item(working, diary_id) if diary_id is not None else None

        if memo_name is None:
            # 没映射过 → 按新增处理（场景：在启用 memos 之前就 add 过）
            if full_item is None:
                raise MemosError(f"id={diary_id} 在远程找不到，无法补建 memo")
            fake_patch = {"item": full_item, "image_files": patch.get("image_files") or []}
            await self._memos_sync_add(memos, fake_patch)
            return

        if full_item is None:
            raise MemosError(f"id={diary_id} 在远程找不到（edit 同步需要完整条目）")

        content = self._format_memo_content(full_item)
        await memos.update_memo_content(memo_name, content)

        image_files = patch.get("image_files") or []
        if image_files:
            attachments: list[str] = []
            for f in image_files:
                local_path = Path(f["local_path"])
                if not local_path.exists():
                    continue
                data = local_path.read_bytes()
                name = await memos.create_attachment(
                    local_path.name, data, mime="image/webp"
                )
                attachments.append(name)
            if attachments:
                await memos.set_memo_attachments(memo_name, attachments)
