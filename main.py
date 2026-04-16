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
    "/diary add                 新增（多轮对话）\n"
    "/diary edit <id>           修改（字段菜单）\n"
    "/diary del <id>            删除（注释掉）\n"
    "/diary restore <id>        恢复已删除\n"
    "/diary diff                查看本地 pending 改动\n"
    "/diary discard             放弃所有 pending\n"
    "/diary push                推送到 GitHub（二次确认）\n"
    "/diary cancel              取消当前多轮对话\n"
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

    async def terminate(self):
        pass

    # ---------------------------------------------------------------- helpers

    def _client(self) -> GithubClient:
        return GithubClient(
            self.config.get("github_token", ""),
            self.config.get("github_repo", ""),
            self.config.get("github_branch", "master"),
        )

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
        self.store.add_patch({"op": "delete", "id": id})
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
        self.store.add_patch({"op": "restore", "id": id})
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
        patch = {"op": "edit", "id": id, "fields": state["patch_fields"]}
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
            commit_sha = await self._do_push()
        except (GithubError, diary_parser.DiaryParseError) as e:
            yield event.plain_result(f"推送失败：{e}")
            return
        except Exception as e:
            logger.exception("diary push failed")
            yield event.plain_result(f"推送异常：{e}")
            return

        yield event.plain_result(f"推送成功 ✅\ncommit: {commit_sha[:10]}")

    def _build_commit_message(self, working: list[dict[str, Any]]) -> str:
        """按 patch 顺序生成提交信息，例如 `diary: 新增日志4# 2026-04-16 19:03:34`。"""
        parts: list[str] = []
        for p in self.store.patches:
            op = p.get("op")
            if op == "add":
                item = p.get("item") or {}
                pid = item.get("id", "?")
                date = item.get("date", "")
                parts.append(f"新增日志{pid}# {date}".rstrip())
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
                parts.append(f"修改日志{pid}# {date}".rstrip())
            elif op == "delete":
                parts.append(f"删除日志{p.get('id', '?')}")
            elif op == "restore":
                parts.append(f"恢复日志{p.get('id', '?')}")
        if not parts:
            return "diary: update"
        return "diary: " + "、".join(parts)

    async def _do_push(self) -> str:
        client = self._client()
        diary_path = self.config.get("diary_file_path", "data/diary.ts")
        content, _ = await client.get_file(diary_path)
        remote = diary_parser.parse(content)
        working = apply_patches(remote, self.store.patches)
        new_content = diary_parser.regenerate(content, working)

        files: list[dict[str, Any]] = [
            {"path": diary_path, "content": new_content.encode("utf-8")}
        ]
        for img in self.store.collect_image_files():
            data = Path(img["local_path"]).read_bytes()
            files.append({"path": img["remote_path"], "content": data})

        message = self._build_commit_message(working)
        commit_sha = await client.commit_files(files, message)

        # cleanup
        for img in self.store.collect_image_files():
            try:
                Path(img["local_path"]).unlink(missing_ok=True)
            except Exception:
                pass
        self.store.clear()
        return commit_sha
