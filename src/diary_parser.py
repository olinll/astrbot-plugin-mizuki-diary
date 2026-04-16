"""解析与重生成 data/diary.ts 中的 diaryData 数组块。

只处理 `const diaryData: DiaryItem[] = [ ... ];` 这一块，其他部分（import、
interface、helper 函数）原样保留。数组内部识别两类元素：

    { ... }            → 正常条目
    /* { ... }, */     → 已删除条目（保留 _deleted=True 标记）

数组内的普通 // 行注释和非对象 /* */ 注释会被忽略（读取时丢弃，写回时不保留）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import json5


_DIARY_RE = re.compile(
    r"const\s+diaryData\s*(?::\s*DiaryItem\s*\[\s*\])?\s*=\s*\["
)

FIELD_ORDER = ["id", "content", "date", "images", "location", "mood", "tags"]
REQUIRED_FIELDS = {"id", "content", "date"}
INDENT = "\t"


class DiaryParseError(Exception):
    pass


def _scan_to_matching(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """从 text[start] 处（应为 open_ch）开始，返回匹配的 close_ch 的位置。

    扫描时跟踪字符串（单/双/反引号）、行注释、块注释，忽略这些上下文里的括号。
    """
    if text[start] != open_ch:
        raise DiaryParseError(f"期望 {open_ch} 在位置 {start}，但看到 {text[start]!r}")
    pos = start + 1
    n = len(text)
    depth = 1
    in_str: str | None = None
    esc = False
    in_line = False
    in_block = False
    while pos < n:
        c = text[pos]
        if in_line:
            if c == "\n":
                in_line = False
        elif in_block:
            if c == "*" and pos + 1 < n and text[pos + 1] == "/":
                in_block = False
                pos += 1
        elif in_str is not None:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == in_str:
                in_str = None
        else:
            if c in ('"', "'", "`"):
                in_str = c
            elif c == "/" and pos + 1 < n:
                nxt = text[pos + 1]
                if nxt == "/":
                    in_line = True
                    pos += 1
                elif nxt == "*":
                    in_block = True
                    pos += 1
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return pos
        pos += 1
    raise DiaryParseError(f"未找到匹配的 {close_ch}")


def _find_block_comment_end(text: str, start: int) -> int:
    """start 是 /* 之后的位置，返回 */ 中 * 的位置。"""
    i = start
    n = len(text)
    while i < n:
        if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
            return i
        i += 1
    raise DiaryParseError("未闭合的块注释 /* ... */")


def find_diary_block(content: str) -> tuple[int, int, int, int]:
    """定位 diaryData 数组。

    返回 (block_start, interior_start, interior_end, block_end)：
    - block_start: `const` 起始位置
    - interior_start: `[` 之后一个字符的位置
    - interior_end: 匹配的 `]` 所在位置
    - block_end: `;` 之后一个字符的位置（若无 `;` 则为 `]` 之后）
    """
    m = _DIARY_RE.search(content)
    if not m:
        raise DiaryParseError("未在文件中找到 `const diaryData = [` 声明")
    block_start = m.start()
    bracket_pos = m.end() - 1
    interior_start = bracket_pos + 1
    interior_end = _scan_to_matching(content, bracket_pos, "[", "]")
    end = interior_end + 1
    n = len(content)
    while end < n and content[end] in " \t":
        end += 1
    if end < n and content[end] == ";":
        end += 1
    return block_start, interior_start, interior_end, end


def _parse_object_literal(text: str) -> dict[str, Any]:
    """解析一段 `{ ... }` 文本。"""
    try:
        return json5.loads(text)
    except Exception as e:
        raise DiaryParseError(f"无法解析对象字面量: {e}\n内容: {text[:200]}")


def parse_items(interior: str) -> list[dict[str, Any]]:
    """解析数组内部文本，返回条目列表（含 _deleted 标记）。"""
    items: list[dict[str, Any]] = []
    pos = 0
    n = len(interior)
    while pos < n:
        c = interior[pos]
        if c.isspace() or c == ",":
            pos += 1
            continue
        if c == "/" and pos + 1 < n and interior[pos + 1] == "/":
            nl = interior.find("\n", pos)
            pos = n if nl == -1 else nl + 1
            continue
        if c == "/" and pos + 1 < n and interior[pos + 1] == "*":
            end = _find_block_comment_end(interior, pos + 2)
            inner = interior[pos + 2 : end].strip()
            if inner.endswith(","):
                inner = inner[:-1].rstrip()
            if inner.startswith("{"):
                obj = _parse_object_literal(inner)
                obj["_deleted"] = True
                items.append(obj)
            pos = end + 2
            continue
        if c == "{":
            obj_end = _scan_to_matching(interior, pos, "{", "}")
            obj_str = interior[pos : obj_end + 1]
            obj = _parse_object_literal(obj_str)
            obj["_deleted"] = False
            items.append(obj)
            pos = obj_end + 1
            continue
        pos += 1
    return items


def parse(content: str) -> list[dict[str, Any]]:
    """解析整个文件，返回条目列表。"""
    _, interior_start, interior_end, _ = find_diary_block(content)
    interior = content[interior_start:interior_end]
    return parse_items(interior)


# ---------- 重生成 ----------

def _format_string(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _format_value(v: Any) -> str:
    if isinstance(v, str):
        return _format_string(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_format_value(x) for x in v) + "]"
    if v is None:
        return "null"
    raise DiaryParseError(f"无法序列化类型 {type(v).__name__}")


def format_item(item: dict[str, Any], indent_lvl: int = 1) -> str:
    pad_outer = INDENT * indent_lvl
    pad_inner = INDENT * (indent_lvl + 1)
    lines: list[str] = []
    for k in FIELD_ORDER:
        if k not in item:
            continue
        v = item[k]
        if k not in REQUIRED_FIELDS:
            if v is None:
                continue
            if isinstance(v, (list, str)) and not v:
                continue
        lines.append(f"{pad_inner}{k}: {_format_value(v)}")
    body = ",\n".join(lines)
    return f"{pad_outer}{{\n{body}\n{pad_outer}}}"


def format_items_block(items: list[dict[str, Any]]) -> str:
    out_lines: list[str] = []
    for item in items:
        clean = {k: v for k, v in item.items() if k != "_deleted"}
        obj_str = format_item(clean, indent_lvl=1)
        if item.get("_deleted"):
            # 在 { 前插入 /* ，在 } 后追加 , */
            stripped = obj_str[len(INDENT):]  # 去掉首行 INDENT
            out_lines.append(f"{INDENT}/* {stripped}, */")
        else:
            out_lines.append(f"{obj_str},")
    return "\n" + "\n".join(out_lines) + "\n"


def regenerate(content: str, items: list[dict[str, Any]]) -> str:
    """用给定的 items 重写 diaryData 块，其他部分保留。"""
    _, interior_start, interior_end, _ = find_diary_block(content)
    new_interior = format_items_block(items)
    return content[:interior_start] + new_interior + content[interior_end:]
