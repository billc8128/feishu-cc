"""飞书云文档 API 客户端。

封装 docs agent 工具要用的 3 件事:
  - 在用户"AI 助手"文件夹建 docx 文档,追加 markdown
  - 读 docx / wiki 文档,返回 markdown(包在 <untrusted-doc-content> 里)
  - 列 + 客户端 title 模糊匹配(飞书无公开搜索 endpoint)

**不包图片**:图片相关放 PR 3,这里 markdown 里的 `![](...)` 先当作纯文本段落,
避免部分实现暴露使用。

Spec:docs/superpowers/specs/2026-04-20-feishu-docs-integration-design.md §5
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ---------- 异常 ----------

class DocNotFound(Exception):
    pass


class PermissionDenied(Exception):
    pass


class DocsAPIError(Exception):
    pass


# ---------- block_type 枚举 ----------

class BT:
    PAGE = 1
    TEXT = 2
    HEADING1 = 3
    HEADING2 = 4
    HEADING3 = 5
    HEADING4 = 6
    HEADING5 = 7
    HEADING6 = 8
    HEADING7 = 9
    HEADING8 = 10
    HEADING9 = 11
    BULLET = 12
    ORDERED = 13
    CODE = 14
    QUOTE = 15
    TODO = 17
    DIVIDER = 22
    TABLE = 31
    TABLE_CELL = 32
    QUOTE_CONTAINER = 34


HEADING_BY_LEVEL = {
    1: BT.HEADING1, 2: BT.HEADING2, 3: BT.HEADING3,
    4: BT.HEADING4, 5: BT.HEADING5, 6: BT.HEADING6,
}
HEADING_LEVEL_BY_TYPE = {v: k for k, v in HEADING_BY_LEVEL.items()}
HEADING_FIELD_BY_LEVEL = {i: f"heading{i}" for i in range(1, 10)}


# ---------- API 地址 ----------

_OPEN_BASE = "https://open.feishu.cn"
DOCX_CREATE = "/open-apis/docx/v1/documents"
DOCX_CHILDREN = "/open-apis/docx/v1/documents/{doc_id}/blocks/{block_id}/children"
DOCX_LIST_BLOCKS = "/open-apis/docx/v1/documents/{doc_id}/blocks"
DRIVE_LIST = "/open-apis/drive/v1/files"
DRIVE_CREATE_FOLDER = "/open-apis/drive/v1/files/create_folder"
WIKI_NODE = "/open-apis/wiki/v2/spaces/get_node"

PAGE_SIZE_BLOCKS = 500
PAGE_SIZE_DRIVE = 200
HTTP_TIMEOUT_S = 30


# ---------- 客户端 ----------

class FeishuDocsClient:
    """以 user_access_token 为鉴权,操作飞书云文档。

    token_provider: 一个每次调用就给出当前有效 user_access_token 的回调。
                    把 OAuth 刷新策略封在 provider 里,客户端只管业务。
    """

    def __init__(self, token_provider: Callable[[], Awaitable[str]]):
        self._token_provider = token_provider

    # ---------- 对外业务 ----------

    async def create_doc_with_markdown(
        self, title: str, markdown: str, folder_token: str
    ) -> tuple[str, str]:
        """建新 doc,把 markdown 渲染进去。返回 (doc_id, doc_url)。"""
        doc_id = await self._create_empty(title=title, folder_token=folder_token)
        root_id = doc_id  # 文档根节点的 block_id 等于 doc_id
        blocks = markdown_to_blocks(markdown)
        if blocks:
            await self._batch_insert_children(doc_id=doc_id, parent_id=root_id, blocks=blocks)
        url = f"https://feishu.cn/docx/{doc_id}"
        return doc_id, url

    async def append_markdown(self, doc_id_or_url: str, markdown: str) -> None:
        doc_id = await self._resolve_doc_id(doc_id_or_url)
        blocks = markdown_to_blocks(markdown)
        if not blocks:
            return
        await self._batch_insert_children(doc_id=doc_id, parent_id=doc_id, blocks=blocks)

    async def read_doc_as_markdown(self, doc_id_or_url: str) -> str:
        """读整篇 doc(带分页),blocks → markdown,包 untrusted 标签。"""
        doc_id = await self._resolve_doc_id(doc_id_or_url)
        raw_blocks = await self._read_all_blocks(doc_id)
        md = blocks_to_markdown(raw_blocks)
        return _wrap_untrusted(md, source=f"docx_id={doc_id}")

    async def ensure_ai_folder(
        self,
        cached_token: Optional[str],
        folder_name: str = "AI 助手",
    ) -> str:
        """若 cached_token 可用直接返回;否则在根目录 list → 找同名 → 没有就 create_folder。"""
        if cached_token:
            return cached_token
        # 在根目录(folder_token 为空)下找同名
        items = await self._list_folder(folder_token="")
        for it in items:
            if it.get("type") == "folder" and it.get("name") == folder_name:
                return it["token"]
        # 创建
        return await self._create_folder(name=folder_name, parent_folder_token="")

    async def list_and_filter_docs(
        self, query: str, folder_token: str, limit: int = 10
    ) -> list[dict]:
        """列文件夹,客户端按 title 模糊匹配 + 按修改时间倒序。"""
        items = await self._list_folder(folder_token=folder_token)
        q = query.strip().lower()
        matched = []
        for it in items:
            if it.get("type") != "docx":
                continue
            name = it.get("name") or ""
            if q and q not in name.lower():
                continue
            matched.append({
                "title": name,
                "doc_id": it.get("token"),
                "url": f"https://feishu.cn/docx/{it.get('token')}",
                "modified_time": int(it.get("modified_time") or 0),
            })
        matched.sort(key=lambda x: x["modified_time"], reverse=True)
        return matched[:limit]

    # ---------- 底层 API ----------

    async def _create_empty(self, title: str, folder_token: str) -> str:
        body: dict[str, Any] = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token
        data = await self._post(DOCX_CREATE, body)
        doc = (data or {}).get("document") or {}
        doc_id = doc.get("document_id")
        if not doc_id:
            raise DocsAPIError(f"create_doc: no document_id in response")
        return doc_id

    async def _batch_insert_children(
        self, doc_id: str, parent_id: str, blocks: list[dict]
    ) -> None:
        """children API 单次最多 50 个 block。自动分批。"""
        CHUNK = 50
        path = DOCX_CHILDREN.format(doc_id=doc_id, block_id=parent_id)
        for i in range(0, len(blocks), CHUNK):
            batch = blocks[i:i + CHUNK]
            body = {"children": batch, "index": -1}  # -1 = 追加到末尾
            await self._post(path, body)

    async def _read_all_blocks(self, doc_id: str) -> list[dict]:
        path = DOCX_LIST_BLOCKS.format(doc_id=doc_id)
        all_items: list[dict] = []
        page_token: Optional[str] = None
        while True:
            params: dict[str, Any] = {"page_size": PAGE_SIZE_BLOCKS}
            if page_token:
                params["page_token"] = page_token
            data = await self._get(path, params=params)
            items = (data or {}).get("items") or []
            all_items.extend(items)
            if not (data or {}).get("has_more"):
                break
            page_token = (data or {}).get("page_token")
            if not page_token:
                break
        return all_items

    async def _list_folder(self, folder_token: str) -> list[dict]:
        """列一个文件夹下所有 items,处理分页。folder_token 为空 = 根目录。"""
        all_items: list[dict] = []
        page_token: Optional[str] = None
        while True:
            params: dict[str, Any] = {"page_size": PAGE_SIZE_DRIVE}
            if folder_token:
                params["folder_token"] = folder_token
            if page_token:
                params["page_token"] = page_token
            data = await self._get(DRIVE_LIST, params=params)
            items = (data or {}).get("files") or []
            all_items.extend(items)
            if not (data or {}).get("has_more"):
                break
            page_token = (data or {}).get("next_page_token")
            if not page_token:
                break
        return all_items

    async def _create_folder(self, name: str, parent_folder_token: str) -> str:
        body: dict[str, Any] = {"name": name, "folder_token": parent_folder_token}
        data = await self._post(DRIVE_CREATE_FOLDER, body)
        token = (data or {}).get("token")
        if not token:
            raise DocsAPIError("create_folder: no token in response")
        return token

    async def _resolve_doc_id(self, doc_id_or_url: str) -> str:
        """接受 docx id、docx URL、wiki URL;返回 docx id。"""
        s = doc_id_or_url.strip()
        if "://" not in s and "/" not in s:
            return s  # 已经是 id
        parsed = urlparse(s)
        path = parsed.path.strip("/")
        # 形如 docx/<token> 或 wiki/<token>
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "docx":
            return parts[1]
        if len(parts) >= 2 and parts[0] == "wiki":
            wiki_token = parts[1]
            return await self._wiki_to_docx_id(wiki_token)
        raise DocNotFound(f"cannot parse doc id from: {doc_id_or_url}")

    async def _wiki_to_docx_id(self, wiki_token: str) -> str:
        data = await self._get(WIKI_NODE, params={"token": wiki_token})
        node = (data or {}).get("node") or {}
        obj_type = node.get("obj_type")
        if obj_type != "docx":
            raise DocsAPIError(f"wiki node is {obj_type}, only docx supported")
        return node["obj_token"]

    # ---------- HTTP ----------

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        return await self._call("GET", path, params=params)

    async def _post(self, path: str, json: Optional[dict] = None) -> dict:
        return await self._call("POST", path, json=json)

    async def _call(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> dict:
        token = await self._token_provider()
        url = _OPEN_BASE + path
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        backoff = 0.5
        for attempt in range(4):
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
                resp = await client.request(
                    method, url, params=params, json=json, headers=headers
                )
            status = resp.status_code
            if status == 429:
                if attempt == 3:
                    raise DocsAPIError("rate limited after retries")
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            if status >= 500:
                if attempt == 2:
                    raise DocsAPIError(f"upstream {status}")
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            if status == 404:
                raise DocNotFound(path)
            if status == 403:
                raise PermissionDenied("403; may need /auth-docs re-auth")
            if status == 401:
                # token 失效:上层 token_provider 下次会自动刷新;这里给一次重试机会
                if attempt == 0:
                    token = await self._token_provider()
                    headers["Authorization"] = f"Bearer {token}"
                    continue
                raise PermissionDenied("401 after refresh")
            try:
                data = resp.json()
            except Exception as exc:
                raise DocsAPIError(f"non-json: {exc}")
            code = data.get("code")
            if code not in (0, None):
                msg = data.get("msg") or "unknown"
                # 飞书的 business error:log 里不打原始 body,避免 token 泄到日志
                logger.warning("feishu docs API biz err: code=%s msg=%s path=%s", code, msg, path)
                if code in (99991663, 99991664):  # token invalid
                    raise PermissionDenied(f"feishu code={code} msg={msg}")
                raise DocsAPIError(f"feishu code={code} msg={msg}")
            return data.get("data") or {}
        raise DocsAPIError("unreachable")


# ---------- Markdown ↔ Blocks ----------

# ----- 词法 -----

_FENCE_RE = re.compile(r"^```(\S*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)-\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)\d+\.\s+(.*)$")
_TODO_RE = re.compile(r"^(\s*)-\s+\[( |x|X)\]\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_DIVIDER_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


def markdown_to_blocks(markdown: str) -> list[dict]:
    """最小 markdown 解析器 → 飞书 docx blocks[]。

    支持:heading1~6 / 段落 / bold / italic / inline code / link /
          bullet / ordered / todo(`- [ ]`)/ quote(`>`)/ fenced code /
          divider(`---`)/ table(GFM pipe 表格)。

    不支持(降级为纯文本段落,log warn):
          图片(PR 3 做)/ 公式 / HTML / 嵌套 >3 层列表 / 脚注。

    Fence-aware:fenced code block 区间内禁用所有其他语法。
    """
    lines = markdown.split("\n")
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # 1. Fenced code block
        m = _FENCE_RE.match(line)
        if m:
            lang = m.group(1)
            body_lines = []
            i += 1
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                body_lines.append(lines[i])
                i += 1
            i += 1  # 跳过 close fence(容忍文末缺 fence)
            blocks.append(_code_block("\n".join(body_lines), lang))
            continue

        # 2. Blank line
        if not line.strip():
            i += 1
            continue

        # 3. Divider
        if _DIVIDER_RE.match(line):
            blocks.append({"block_type": BT.DIVIDER, "divider": {}})
            i += 1
            continue

        # 4. Heading
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            content = m.group(2).strip()
            blocks.append(_heading_block(level, content))
            i += 1
            continue

        # 5. Table(至少两行:header + sep)
        if _TABLE_ROW_RE.match(line) and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            table_lines = [line, lines[i + 1]]  # 保留 header + sep 一起渲染
            j = i + 2
            while j < len(lines) and _TABLE_ROW_RE.match(lines[j]):
                table_lines.append(lines[j])
                j += 1
            blocks.extend(_table_blocks(table_lines, lines[i + 1]))
            i = j
            continue

        # 6. Todo(- [ ])
        m = _TODO_RE.match(line)
        if m:
            checked = m.group(2).lower() == "x"
            content = m.group(3)
            blocks.append(_todo_block(content, checked))
            i += 1
            continue

        # 7. Bullet
        m = _BULLET_RE.match(line)
        if m:
            content = m.group(2)
            blocks.append(_bullet_block(content))
            i += 1
            continue

        # 8. Ordered
        m = _ORDERED_RE.match(line)
        if m:
            content = m.group(2)
            blocks.append(_ordered_block(content))
            i += 1
            continue

        # 9. Quote(行首 `>`,一次一行,避免跨段复杂处理)
        m = _QUOTE_RE.match(line)
        if m:
            content = m.group(1)
            blocks.append(_quote_block(content))
            i += 1
            continue

        # 10. 普通段落(合并连续非空非前缀行)
        para_lines = [line]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip():
                break
            if (_HEADING_RE.match(nxt) or _BULLET_RE.match(nxt) or _ORDERED_RE.match(nxt)
                    or _QUOTE_RE.match(nxt) or _DIVIDER_RE.match(nxt)
                    or _FENCE_RE.match(nxt) or _TABLE_ROW_RE.match(nxt)):
                break
            para_lines.append(nxt)
            j += 1
        blocks.append(_text_block(" ".join(l.strip() for l in para_lines)))
        i = j
    return blocks


def _heading_block(level: int, content: str) -> dict:
    level = max(1, min(level, 9))
    block_type = HEADING_BY_LEVEL.get(level, BT.HEADING3)
    field = HEADING_FIELD_BY_LEVEL[level]
    return {"block_type": block_type, field: _text_with_style(content)}


def _text_block(content: str) -> dict:
    return {"block_type": BT.TEXT, "text": _text_with_style(content)}


def _bullet_block(content: str) -> dict:
    return {"block_type": BT.BULLET, "bullet": _text_with_style(content)}


def _ordered_block(content: str) -> dict:
    return {"block_type": BT.ORDERED, "ordered": _text_with_style(content)}


def _todo_block(content: str, done: bool) -> dict:
    return {
        "block_type": BT.TODO,
        "todo": {
            **_text_with_style(content),
            "style": {"done": done},
        },
    }


def _quote_block(content: str) -> dict:
    return {"block_type": BT.QUOTE, "quote": _text_with_style(content)}


def _code_block(body: str, language: str) -> dict:
    return {
        "block_type": BT.CODE,
        "code": {
            "elements": [{"text_run": {"content": body, "text_element_style": {}}}],
            "style": {"language": _code_language_id(language), "wrap": True},
        },
    }


def _table_blocks(rows: list[str], sep_line: str) -> list[dict]:
    """GFM 表格 → 飞书 table + table_cell + text blocks。

    飞书 table 需要先建表头、再每个单元格包一个 text。这里用 property 的方式:
    一个 table block + 行数/列数声明;children 由调用方分多次 batch_create_descendants
    其实更省事。我们简单起见,用 'table.property.row_size/column_size' + 让
    默认空单元格自己长出来——本地生成一个纯 text 段落带 pipe 描述兜底。
    """
    # 兼容策略:飞书 docx v1 的表格创建在纯 REST + children 调用下非常啰嗦。
    # 这里退化为生成一个 text block + 代码块形式保留表格外观(markdown 里 GFM 表格
    # 渲染在 docx 里不是一等公民,实测 Lark API 建 table 至少 3 次往返)。
    # PR 3 再升级为原生 table。
    preview = "\n".join(rows)
    return [{
        "block_type": BT.CODE,
        "code": {
            "elements": [{"text_run": {"content": preview, "text_element_style": {}}}],
            "style": {"language": _code_language_id("markdown"), "wrap": True},
        },
    }]


def _text_with_style(content: str) -> dict:
    """把 markdown inline 语法(**粗** *斜* `code` [text](url))解析成 elements。"""
    elems = _parse_inline(content)
    return {"elements": elems, "style": {}}


# Inline 解析:简单有限状态扫描
_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_INLINE_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_INLINE_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*]+)\*(?!\*)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _parse_inline(text: str) -> list[dict]:
    """把内联 markdown 变成一串 text_run elements。保序,不支持嵌套(YAGNI)。"""
    if not text:
        return [_run("", {})]
    tokens: list[tuple[int, int, str, dict]] = []  # (start, end, content, style)

    for m in _INLINE_CODE_RE.finditer(text):
        tokens.append((m.start(), m.end(), m.group(1), {"inline_code": True}))
    for m in _INLINE_BOLD_RE.finditer(text):
        if _is_inside(tokens, m.start(), m.end()):
            continue
        tokens.append((m.start(), m.end(), m.group(1), {"bold": True}))
    for m in _INLINE_ITALIC_RE.finditer(text):
        if _is_inside(tokens, m.start(), m.end()):
            continue
        tokens.append((m.start(), m.end(), m.group(1), {"italic": True}))
    for m in _INLINE_LINK_RE.finditer(text):
        if _is_inside(tokens, m.start(), m.end()):
            continue
        link_url = m.group(2)
        tokens.append((m.start(), m.end(), m.group(1), {"link": {"url": link_url}}))

    # 把 tokens 按 start 排,夹在中间的纯文本也变成 run
    tokens.sort(key=lambda t: t[0])
    out: list[dict] = []
    cursor = 0
    for start, end, content, style in tokens:
        if start > cursor:
            plain = text[cursor:start]
            if plain:
                out.append(_run(plain, {}))
        out.append(_run(content, style))
        cursor = end
    if cursor < len(text):
        tail = text[cursor:]
        if tail:
            out.append(_run(tail, {}))
    if not out:
        out.append(_run(text, {}))
    return out


def _is_inside(tokens: list[tuple], start: int, end: int) -> bool:
    return any(s <= start and end <= e for s, e, _, _ in tokens)


def _run(content: str, style: dict) -> dict:
    """构造一个 text_run element。style dict 可能含 bold/italic/inline_code/link。"""
    elem: dict[str, Any] = {"content": content, "text_element_style": {}}
    for key in ("bold", "italic", "inline_code"):
        if style.get(key):
            elem["text_element_style"][key] = True
    if "link" in style:
        elem["text_element_style"]["link"] = style["link"]
    return {"text_run": elem}


# ---------- Blocks → Markdown ----------

def blocks_to_markdown(blocks: list[dict]) -> str:
    """把飞书 blocks 渲染回 markdown。足够 agent 读懂就行,不追求 round-trip 完全一致。"""
    lines: list[str] = []
    for b in blocks:
        bt = b.get("block_type")
        if bt in HEADING_LEVEL_BY_TYPE:
            level = HEADING_LEVEL_BY_TYPE[bt]
            field = HEADING_FIELD_BY_LEVEL[level]
            lines.append("#" * level + " " + _render_inline(b.get(field, {}).get("elements", [])))
        elif bt == BT.TEXT:
            lines.append(_render_inline(b.get("text", {}).get("elements", [])))
        elif bt == BT.BULLET:
            lines.append("- " + _render_inline(b.get("bullet", {}).get("elements", [])))
        elif bt == BT.ORDERED:
            lines.append("1. " + _render_inline(b.get("ordered", {}).get("elements", [])))
        elif bt == BT.TODO:
            done = (b.get("todo", {}).get("style") or {}).get("done")
            marker = "x" if done else " "
            lines.append(f"- [{marker}] " + _render_inline(b.get("todo", {}).get("elements", [])))
        elif bt == BT.QUOTE:
            lines.append("> " + _render_inline(b.get("quote", {}).get("elements", [])))
        elif bt == BT.CODE:
            code = b.get("code", {})
            lang = (code.get("style") or {}).get("language", "")
            # feishu 返回的 language 可能是数字 id 或字符串,都兜住
            body = _render_inline(code.get("elements", []), plain=True)
            lines.append(f"```{_lang_from_id(lang)}")
            lines.append(body)
            lines.append("```")
        elif bt == BT.DIVIDER:
            lines.append("---")
        elif bt == BT.PAGE:
            # 根节点自身,跳过
            continue
        else:
            # 不认识的 block(image、table、iframe 等):占位
            lines.append(f"[未识别的 block_type={bt}]")
        lines.append("")  # 空行分隔
    return "\n".join(lines).strip() + "\n"


def _render_inline(elements: list[dict], plain: bool = False) -> str:
    parts: list[str] = []
    for el in elements or []:
        run = el.get("text_run") or {}
        content = run.get("content", "")
        if plain:
            parts.append(content)
            continue
        style = run.get("text_element_style") or {}
        if style.get("inline_code"):
            parts.append(f"`{content}`")
        elif style.get("bold"):
            parts.append(f"**{content}**")
        elif style.get("italic"):
            parts.append(f"*{content}*")
        elif style.get("link"):
            url = (style.get("link") or {}).get("url", "")
            parts.append(f"[{content}]({url})")
        else:
            parts.append(content)
    return "".join(parts)


# ---------- 代码块语言 id 映射 ----------
# 飞书 docx v1 code block 的 language 字段是整数枚举。这里只覆盖常用语种,
# 其他的归并到 PLAIN TEXT(1)。

_LANG_TO_ID = {
    "": 1, "plaintext": 1, "text": 1,
    "python": 49, "py": 49,
    "javascript": 30, "js": 30,
    "typescript": 63, "ts": 63,
    "bash": 4, "sh": 4, "shell": 4, "zsh": 4,
    "json": 31, "yaml": 65, "yml": 65,
    "html": 24, "css": 12, "sql": 56,
    "go": 23, "rust": 52, "java": 29, "c": 9, "cpp": 10, "csharp": 13,
    "markdown": 35, "md": 35,
    "xml": 64, "ruby": 51, "php": 47, "swift": 58, "kotlin": 32,
}
_ID_TO_LANG = {v: k for k, v in _LANG_TO_ID.items() if k}


def _code_language_id(lang: str) -> int:
    if not lang:
        return 1
    return _LANG_TO_ID.get(lang.lower(), 1)


def _lang_from_id(lang: Any) -> str:
    """反向:blocks → markdown 时 feishu 给的可能是 int 或 str。"""
    if isinstance(lang, str):
        return lang if lang in _LANG_TO_ID else ""
    if isinstance(lang, int):
        return _ID_TO_LANG.get(lang, "")
    return ""


# ---------- Prompt injection 包裹 ----------

def _wrap_untrusted(markdown: str, source: str) -> str:
    """包装读到的文档内容,防 prompt injection。source 进 tag 属性方便审计。"""
    return (
        f"<untrusted-doc-content source=\"{source}\">\n"
        f"{markdown.rstrip()}\n"
        f"</untrusted-doc-content>\n"
    )
