"""PR 2 — docs_client 测试。

覆盖:
  - markdown → blocks:heading / 段落 / inline / bullet / ordered / todo / quote / code / divider / fence-aware
  - blocks → markdown:round-trip 合理近似
  - <untrusted-doc-content> 包裹
  - HTTP 重试/鉴权/错误路径(mock httpx)
  - 分页(_read_all_blocks, _list_folder)
  - URL 解析(docx/wiki/裸 id)
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "t")
os.environ.setdefault("FEISHU_APP_ID", "c")
os.environ.setdefault("FEISHU_APP_SECRET", "s")


from feishu.docs_client import (  # noqa: E402
    BT,
    DocNotFound,
    DocsAPIError,
    FeishuDocsClient,
    PermissionDenied,
    _wrap_untrusted,
    blocks_to_markdown,
    markdown_to_blocks,
)


class MarkdownToBlocksTests(unittest.TestCase):

    def test_heading_levels(self) -> None:
        blocks = markdown_to_blocks("# H1\n## H2\n### H3\n#### H4")
        types = [b["block_type"] for b in blocks]
        self.assertEqual(types, [BT.HEADING1, BT.HEADING2, BT.HEADING3, BT.HEADING4])

    def test_plain_paragraph_joins_consecutive_lines(self) -> None:
        md = "这是第一行\n还是同一段\n\n这是新段落"
        blocks = markdown_to_blocks(md)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["block_type"], BT.TEXT)
        txt = blocks[0]["text"]["elements"][0]["text_run"]["content"]
        self.assertIn("第一行", txt)
        self.assertIn("还是同一段", txt)

    def test_inline_styles(self) -> None:
        blocks = markdown_to_blocks("**粗** 正常 *斜* `code` [L](https://x.com)")
        self.assertEqual(len(blocks), 1)
        elems = blocks[0]["text"]["elements"]
        styled = [(e["text_run"]["content"], e["text_run"]["text_element_style"]) for e in elems]
        has_bold = any(s.get("bold") for _, s in styled)
        has_italic = any(s.get("italic") for _, s in styled)
        has_code = any(s.get("inline_code") for _, s in styled)
        has_link = any("link" in s for _, s in styled)
        self.assertTrue(has_bold and has_italic and has_code and has_link)

    def test_bullet_list(self) -> None:
        blocks = markdown_to_blocks("- item A\n- item B")
        self.assertEqual([b["block_type"] for b in blocks], [BT.BULLET, BT.BULLET])

    def test_ordered_list(self) -> None:
        blocks = markdown_to_blocks("1. a\n2. b\n3. c")
        self.assertEqual([b["block_type"] for b in blocks], [BT.ORDERED, BT.ORDERED, BT.ORDERED])

    def test_todo_checked_vs_unchecked(self) -> None:
        blocks = markdown_to_blocks("- [ ] A\n- [x] B\n- [X] C")
        types = [b["block_type"] for b in blocks]
        self.assertEqual(types, [BT.TODO, BT.TODO, BT.TODO])
        dones = [b["todo"]["style"]["done"] for b in blocks]
        self.assertEqual(dones, [False, True, True])

    def test_quote(self) -> None:
        blocks = markdown_to_blocks("> 一段引用")
        self.assertEqual(blocks[0]["block_type"], BT.QUOTE)

    def test_divider(self) -> None:
        blocks = markdown_to_blocks("---\n***\n___")
        self.assertEqual([b["block_type"] for b in blocks], [BT.DIVIDER, BT.DIVIDER, BT.DIVIDER])

    def test_fenced_code_preserves_content(self) -> None:
        md = "```python\ndef f():\n    return 1\n```"
        blocks = markdown_to_blocks(md)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["block_type"], BT.CODE)
        body = blocks[0]["code"]["elements"][0]["text_run"]["content"]
        self.assertIn("def f()", body)

    def test_fence_aware_disables_inner_rules(self) -> None:
        """fence 内的 `# foo` 不能变 heading,`- bar` 不能变 bullet。"""
        md = "```\n# not heading\n- not bullet\n> not quote\n```"
        blocks = markdown_to_blocks(md)
        self.assertEqual(len(blocks), 1, blocks)
        self.assertEqual(blocks[0]["block_type"], BT.CODE)

    def test_unterminated_fence_is_tolerated(self) -> None:
        """用户少写 close fence 时不崩,把剩余全当 code body。"""
        md = "```\nhello\nworld"
        blocks = markdown_to_blocks(md)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["block_type"], BT.CODE)

    def test_mixed_document(self) -> None:
        md = (
            "# 标题\n\n"
            "一段话。\n\n"
            "- 点 1\n- 点 2\n\n"
            "```python\nprint('hi')\n```\n\n"
            "> 引用\n"
            "---\n"
        )
        blocks = markdown_to_blocks(md)
        types = [b["block_type"] for b in blocks]
        self.assertIn(BT.HEADING1, types)
        self.assertIn(BT.TEXT, types)
        self.assertIn(BT.BULLET, types)
        self.assertIn(BT.CODE, types)
        self.assertIn(BT.QUOTE, types)
        self.assertIn(BT.DIVIDER, types)

    def test_empty_input(self) -> None:
        self.assertEqual(markdown_to_blocks(""), [])
        self.assertEqual(markdown_to_blocks("\n\n  \n"), [])


class RoundTripTests(unittest.TestCase):

    def _roundtrip(self, md: str) -> str:
        return blocks_to_markdown(markdown_to_blocks(md))

    def test_headings_roundtrip(self) -> None:
        out = self._roundtrip("# A\n## B\n### C")
        self.assertIn("# A", out)
        self.assertIn("## B", out)
        self.assertIn("### C", out)

    def test_todo_roundtrip(self) -> None:
        out = self._roundtrip("- [ ] X\n- [x] Y")
        self.assertIn("- [ ] X", out)
        self.assertIn("- [x] Y", out)

    def test_code_roundtrip(self) -> None:
        out = self._roundtrip("```python\nprint(1)\n```")
        self.assertIn("```", out)
        self.assertIn("print(1)", out)

    def test_inline_bold_survives(self) -> None:
        out = self._roundtrip("这里 **重点** 结束")
        self.assertIn("**重点**", out)


class WrapUntrustedTests(unittest.TestCase):

    def test_wraps_and_tags_source(self) -> None:
        out = _wrap_untrusted("# hello", source="docx_id=ABC")
        self.assertIn("<untrusted-doc-content", out)
        self.assertIn('source="docx_id=ABC"', out)
        self.assertIn("# hello", out)
        self.assertIn("</untrusted-doc-content>", out)


class AIOriginBannerTests(unittest.TestCase):
    """新建文档首行"AI 生成"标识的渲染正确性。"""

    def test_banner_renders_as_quote_block(self) -> None:
        from agent.tools_docs import _ai_origin_banner
        from feishu.docs_client import BT

        banner = _ai_origin_banner()
        # 放到 markdown 最前,确保被识别为 quote block
        blocks = markdown_to_blocks(banner + "# 正文标题\n\n内容")
        self.assertGreaterEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["block_type"], BT.QUOTE)

        # quote 内容应包含 "AI 助手" 字样
        quote_text = blocks[0]["quote"]["elements"][0]["text_run"]["content"]
        self.assertIn("AI 助手", quote_text)

    def test_banner_includes_timestamp(self) -> None:
        from agent.tools_docs import _ai_origin_banner
        import re

        banner = _ai_origin_banner()
        # YYYY-MM-DD HH:MM 格式时间戳必须存在
        self.assertRegex(banner, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


class HTTPPathTests(unittest.TestCase):
    """Mock httpx 测试 _call 的重试/错误映射。"""

    def _build_client(self) -> FeishuDocsClient:
        async def tok():
            return "tok_abc"
        return FeishuDocsClient(token_provider=tok)

    def _mock_response(self, status: int, json_body: dict | None = None):
        m = MagicMock()
        m.status_code = status
        m.json = MagicMock(return_value=json_body or {})
        return m

    def test_401_triggers_token_refresh_and_retry(self) -> None:
        call_count = [0]

        async def tok():
            call_count[0] += 1
            return f"tok_{call_count[0]}"

        client = FeishuDocsClient(token_provider=tok)

        responses = [
            self._mock_response(401),
            self._mock_response(200, {"code": 0, "data": {"ok": True}}),
        ]

        async def fake_request(method, url, **kwargs):
            return responses.pop(0)

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def request(self, *a, **kw): return await fake_request(*a, **kw)

        async def go():
            with patch("feishu.docs_client.httpx.AsyncClient", return_value=FakeClient()):
                return await client._get("/x")

        result = asyncio.run(go())
        self.assertEqual(result, {"ok": True})
        self.assertEqual(call_count[0], 2, "token_provider 应被调 2 次(初始 + 401 后)")

    def test_404_raises_docnotfound(self) -> None:
        client = self._build_client()

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def request(self, *a, **kw):
                m = MagicMock(); m.status_code = 404
                m.json = MagicMock(return_value={})
                return m

        async def go():
            with patch("feishu.docs_client.httpx.AsyncClient", return_value=FakeClient()):
                await client._get("/x")

        with self.assertRaises(DocNotFound):
            asyncio.run(go())

    def test_403_raises_permission(self) -> None:
        client = self._build_client()

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def request(self, *a, **kw):
                m = MagicMock(); m.status_code = 403
                m.json = MagicMock(return_value={})
                return m

        async def go():
            with patch("feishu.docs_client.httpx.AsyncClient", return_value=FakeClient()):
                await client._get("/x")

        with self.assertRaises(PermissionDenied):
            asyncio.run(go())

    def test_biz_error_code_raises(self) -> None:
        client = self._build_client()

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def request(self, *a, **kw):
                m = MagicMock(); m.status_code = 200
                m.json = MagicMock(return_value={"code": 12345, "msg": "bad thing"})
                return m

        async def go():
            with patch("feishu.docs_client.httpx.AsyncClient", return_value=FakeClient()):
                await client._get("/x")

        with self.assertRaisesRegex(DocsAPIError, "12345"):
            asyncio.run(go())


class PaginationTests(unittest.TestCase):

    def test_read_all_blocks_follows_page_token(self) -> None:
        async def tok():
            return "t"
        client = FeishuDocsClient(token_provider=tok)

        pages = [
            {"code": 0, "data": {"items": [{"block_id": "b1"}, {"block_id": "b2"}],
                                 "has_more": True, "page_token": "P2"}},
            {"code": 0, "data": {"items": [{"block_id": "b3"}],
                                 "has_more": False}},
        ]

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def request(self, *a, **kw):
                m = MagicMock(); m.status_code = 200
                m.json = MagicMock(return_value=pages.pop(0))
                return m

        async def go():
            with patch("feishu.docs_client.httpx.AsyncClient", return_value=FakeClient()):
                return await client._read_all_blocks("doc_id")

        blocks = asyncio.run(go())
        self.assertEqual(len(blocks), 3)
        self.assertEqual([b["block_id"] for b in blocks], ["b1", "b2", "b3"])


class DocIdResolutionTests(unittest.TestCase):

    def test_bare_id(self) -> None:
        async def tok(): return "t"
        client = FeishuDocsClient(token_provider=tok)
        result = asyncio.run(client._resolve_doc_id("ABCxyz123"))
        self.assertEqual(result, "ABCxyz123")

    def test_docx_url(self) -> None:
        async def tok(): return "t"
        client = FeishuDocsClient(token_provider=tok)
        result = asyncio.run(client._resolve_doc_id("https://feishu.cn/docx/XYZ789"))
        self.assertEqual(result, "XYZ789")

    def test_wiki_url_calls_get_node(self) -> None:
        async def tok(): return "t"
        client = FeishuDocsClient(token_provider=tok)

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def request(self, *a, **kw):
                m = MagicMock(); m.status_code = 200
                m.json = MagicMock(return_value={
                    "code": 0,
                    "data": {"node": {"obj_type": "docx", "obj_token": "DOCX_FROM_WIKI"}},
                })
                return m

        async def go():
            with patch("feishu.docs_client.httpx.AsyncClient", return_value=FakeClient()):
                return await client._resolve_doc_id("https://feishu.cn/wiki/WIKI123")

        self.assertEqual(asyncio.run(go()), "DOCX_FROM_WIKI")


class ImageBlockParsingTests(unittest.TestCase):
    """纯解析器层:图片占位的识别与段落隔离。"""

    def test_image_on_its_own_line_is_pending(self) -> None:
        from feishu.docs_client import _is_pending_image

        blocks = markdown_to_blocks("![架构图](diagrams/arch.png)")
        self.assertEqual(len(blocks), 1)
        self.assertTrue(_is_pending_image(blocks[0]))
        self.assertEqual(blocks[0]["__pending_image__"]["alt"], "架构图")
        self.assertEqual(blocks[0]["__pending_image__"]["path"], "diagrams/arch.png")

    def test_image_breaks_paragraph(self) -> None:
        from feishu.docs_client import _is_pending_image, BT

        md = "第一段\n![x](a.png)\n第二段"
        blocks = markdown_to_blocks(md)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["block_type"], BT.TEXT)
        self.assertTrue(_is_pending_image(blocks[1]))
        self.assertEqual(blocks[2]["block_type"], BT.TEXT)

    def test_inline_image_in_paragraph_is_plain_text(self) -> None:
        from feishu.docs_client import BT, _is_pending_image

        md = "看这个图 ![x](a.png) 挺好"
        blocks = markdown_to_blocks(md)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["block_type"], BT.TEXT)
        self.assertFalse(_is_pending_image(blocks[0]))

    def test_image_inside_fenced_code_is_not_detected(self) -> None:
        md = "```\n![x](bomb.png)\n```"
        blocks = markdown_to_blocks(md)
        self.assertEqual(len(blocks), 1)
        from feishu.docs_client import BT
        self.assertEqual(blocks[0]["block_type"], BT.CODE)


class ImageInsertionTests(unittest.TestCase):
    """_insert_with_images:图片上传 + 降级的副作用行为。

    用 fake httpx 拦截所有 HTTP,断言顺序和降级。
    """

    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.sandbox = __import__("pathlib").Path(self._tmp.name).resolve()
        # 准备一张合法图片
        (self.sandbox / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_client_with_fake_http(self, response_script: list):
        """response_script: 每次 request 返回 (status, json) 的列表,按顺序消费。"""
        async def tok():
            return "t"
        client = FeishuDocsClient(token_provider=tok)
        seen = []

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def request(self, method, url, **kw):
                seen.append((method, url, kw))
                status, body = response_script.pop(0)
                m = MagicMock(); m.status_code = status
                m.json = MagicMock(return_value=body)
                return m
            async def post(self, url, **kw):
                seen.append(("POST", url, kw))
                status, body = response_script.pop(0)
                m = MagicMock(); m.status_code = status
                m.json = MagicMock(return_value=body)
                return m

        return client, FakeClient, seen

    def test_image_upload_and_replace_sequence(self) -> None:
        """正常路径:占位 image → upload_all → replace_image 三次调用的顺序。"""
        from feishu.docs_client import _pending_image_block

        # 响应顺序:
        # 1. text block 批量插入(普通段落 before)
        # 2. 占位 image block 创建(返回 block_id)
        # 3. upload_all(返回 file_token)
        # 4. replace_image PATCH
        # 5. text block 批量插入(普通段落 after)
        script = [
            (200, {"code": 0, "data": {}}),  # 1
            (200, {"code": 0, "data": {"children": [{"block_id": "img_block_9"}]}}),  # 2
            (200, {"code": 0, "data": {"file_token": "tok_pic"}}),  # 3
            (200, {"code": 0, "data": {}}),  # 4
            (200, {"code": 0, "data": {}}),  # 5
        ]
        client, FakeClient, seen = self._make_client_with_fake_http(script)

        blocks = [
            {"block_type": 2, "text": {"elements": [{"text_run": {"content": "before", "text_element_style": {}}}], "style": {}}},
            _pending_image_block(alt="pic", path="pic.png"),
            {"block_type": 2, "text": {"elements": [{"text_run": {"content": "after", "text_element_style": {}}}], "style": {}}},
        ]

        async def go():
            with patch("feishu.docs_client.httpx.AsyncClient", return_value=FakeClient()):
                await client._insert_with_images(
                    doc_id="DOC", parent_id="DOC",
                    blocks=blocks, sandbox_root=self.sandbox,
                )

        asyncio.run(go())
        # 顺序验证
        self.assertEqual(len(seen), 5)
        self.assertIn("blocks/DOC/children", seen[0][1])  # before flush
        self.assertIn("blocks/DOC/children", seen[1][1])  # image placeholder
        self.assertIn("medias/upload_all", seen[2][1])    # upload
        self.assertEqual(seen[3][0], "PATCH")             # replace
        self.assertIn("blocks/DOC/children", seen[4][1])  # after flush

    def test_image_outside_sandbox_falls_back_to_text(self) -> None:
        """路径越界:不触网,插入纯文本占位。"""
        from feishu.docs_client import _pending_image_block

        # 只有一次:text block 批量插入(含降级后的 [图片: ...] 文本)
        script = [(200, {"code": 0, "data": {}})]
        client, FakeClient, seen = self._make_client_with_fake_http(script)

        blocks = [_pending_image_block(alt="x", path="/etc/passwd")]

        async def go():
            with patch("feishu.docs_client.httpx.AsyncClient", return_value=FakeClient()):
                await client._insert_with_images(
                    doc_id="DOC", parent_id="DOC",
                    blocks=blocks, sandbox_root=self.sandbox,
                )

        asyncio.run(go())
        # 只调一次 children API,且内容是 text block 包 [图片: x]
        self.assertEqual(len(seen), 1)
        req_body = seen[0][2].get("json") or {}
        children = req_body.get("children", [])
        self.assertEqual(len(children), 1)
        elems = children[0]["text"]["elements"]
        content = elems[0]["text_run"]["content"]
        self.assertIn("图片", content)
        self.assertIn("x", content)

    def test_no_sandbox_root_falls_back_to_text(self) -> None:
        from feishu.docs_client import _pending_image_block

        script = [(200, {"code": 0, "data": {}})]
        client, FakeClient, seen = self._make_client_with_fake_http(script)
        blocks = [_pending_image_block(alt="x", path="pic.png")]

        async def go():
            with patch("feishu.docs_client.httpx.AsyncClient", return_value=FakeClient()):
                await client._insert_with_images(
                    doc_id="DOC", parent_id="DOC",
                    blocks=blocks, sandbox_root=None,
                )

        asyncio.run(go())
        self.assertEqual(len(seen), 1)  # 就一次 text 插入,没有 upload


if __name__ == "__main__":
    unittest.main()
