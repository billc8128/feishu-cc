"""PR 1 — OAuth 模块的单元测试。

覆盖:
  - schema 自动创建
  - state 生命周期(create / consume / 复用 / 过期 / bogus)
  - build_authorize_url 含 scope/state/redirect_uri
  - token 落库和读取,docs_folder_name 默认值
  - get_valid_token 各状态:有效 / 未授权 / refresh 过期
  - 单飞锁:并发刷新只触发一次网络调用(双检生效)
  - token_suffix 不泄露完整 token
  - _normalize_token_payload 兼容字段名差异
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "cli_test")
os.environ.setdefault("FEISHU_APP_SECRET", "test-secret")
os.environ.setdefault("RAILWAY_PUBLIC_DOMAIN", "test.example.com")

from config import settings  # noqa: E402


class OAuthModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_data_dir = settings.data_dir
        self._tmp = tempfile.TemporaryDirectory()
        settings.data_dir = self._tmp.name

        import feishu.oauth as oauth
        # 强制重置模块级 schema flag + 锁字典(测试隔离)
        oauth._schema_ready = False
        oauth._refresh_locks.clear()
        self.oauth = oauth

    def tearDown(self) -> None:
        settings.data_dir = self._orig_data_dir
        self._tmp.cleanup()

    # ---------- state ----------

    def test_state_lifecycle(self) -> None:
        s = self.oauth.create_state("ou_alice")
        got = self.oauth.consume_state(s)
        self.assertEqual(got, "ou_alice")

        # 复用同一 state 应被拦截
        with self.assertRaises(self.oauth.OAuthStateInvalid):
            self.oauth.consume_state(s)

    def test_bogus_state_rejected(self) -> None:
        with self.assertRaises(self.oauth.OAuthStateInvalid):
            self.oauth.consume_state("i-never-existed")

    def test_expired_state_rejected(self) -> None:
        self.oauth._ensure_schema()
        now = int(time.time())
        with sqlite3.connect(settings.sqlite_path) as c:
            c.execute(
                "INSERT INTO feishu_oauth_states VALUES (?,?,?,?)",
                ("stale", "ou_u", now - 1000, now - 500),
            )
        with self.assertRaises(self.oauth.OAuthStateInvalid):
            self.oauth.consume_state("stale")

    def test_authorize_url_has_required_params(self) -> None:
        url = self.oauth.build_authorize_url("ou_bob")
        self.assertIn("accounts.feishu.cn", url)
        # 不硬编码 client_id 的值,其他测试可能已经改过 env;只校验参数存在
        self.assertIn("client_id=", url)
        self.assertIn(f"client_id={settings.feishu_app_id}", url)
        self.assertIn("response_type=code", url)
        self.assertIn("offline_access", url)
        self.assertIn("test.example.com", url)
        self.assertIn("state=", url)

    # ---------- token store ----------

    def test_save_and_read_token(self) -> None:
        now = int(time.time())
        self.oauth.save_token("ou_c", "acc", "ref", now + 7200, now + 30 * 86400)
        row = self.oauth.read_token("ou_c")
        self.assertIsNotNone(row)
        self.assertEqual(row.access_token, "acc")
        self.assertEqual(row.refresh_token, "ref")
        self.assertEqual(row.docs_folder_name, "AI 助手")
        self.assertIsNone(row.docs_folder_token)

    def test_save_token_is_upsert(self) -> None:
        now = int(time.time())
        self.oauth.save_token("ou_d", "a1", "r1", now + 100, now + 200)
        self.oauth.save_token("ou_d", "a2", "r2", now + 300, now + 400)
        row = self.oauth.read_token("ou_d")
        self.assertEqual(row.access_token, "a2")
        self.assertEqual(row.refresh_token, "r2")

    def test_folder_token_save(self) -> None:
        now = int(time.time())
        self.oauth.save_token("ou_e", "a", "r", now + 100, now + 200)
        self.oauth.save_folder_token("ou_e", "folder_xyz")
        row = self.oauth.read_token("ou_e")
        self.assertEqual(row.docs_folder_token, "folder_xyz")

    # ---------- get_valid_token ----------

    def test_get_valid_token_no_user(self) -> None:
        async def go() -> None:
            with self.assertRaises(self.oauth.NotAuthorized):
                await self.oauth.get_valid_token("ou_missing")
        asyncio.run(go())

    def test_get_valid_token_returns_live(self) -> None:
        now = int(time.time())
        self.oauth.save_token("ou_live", "acc_live", "r", now + 7200, now + 30 * 86400)

        async def go() -> str:
            return await self.oauth.get_valid_token("ou_live")

        self.assertEqual(asyncio.run(go()), "acc_live")

    def test_get_valid_token_refresh_expired(self) -> None:
        now = int(time.time())
        self.oauth.save_token("ou_exp", "a", "r", now - 100, now - 50)

        async def go() -> None:
            with self.assertRaises(self.oauth.NotAuthorized):
                await self.oauth.get_valid_token("ou_exp")
        asyncio.run(go())

    def test_get_valid_token_triggers_refresh_when_near_expiry(self) -> None:
        """access 剩 60s 但 refresh 活着 → 触发刷新,拿到新 access。"""
        now = int(time.time())
        # access 60 秒后过期(在 REFRESH_PREEMPT_SECONDS=300 阈值内)
        self.oauth.save_token("ou_r", "old_acc", "old_ref", now + 60, now + 30 * 86400)

        refresh_calls = []

        async def fake_refresh(refresh_token):
            refresh_calls.append(refresh_token)
            return {
                "access_token": "new_acc",
                "refresh_token": "new_ref",
                "expires_in": 7200,
                "refresh_token_expires_in": 30 * 86400,
            }

        async def go() -> str:
            with patch.object(self.oauth, "_refresh", side_effect=fake_refresh):
                return await self.oauth.get_valid_token("ou_r")

        got = asyncio.run(go())
        self.assertEqual(got, "new_acc")
        self.assertEqual(len(refresh_calls), 1)
        self.assertEqual(refresh_calls[0], "old_ref")

        # DB 应持久化新 token
        row = self.oauth.read_token("ou_r")
        self.assertEqual(row.access_token, "new_acc")
        self.assertEqual(row.refresh_token, "new_ref")

    def test_concurrent_refresh_single_flight(self) -> None:
        """5 个协程同时请求时,_refresh 网络调用只触发 1 次(双检 + 锁)。"""
        now = int(time.time())
        self.oauth.save_token("ou_race", "old_a", "old_r", now + 30, now + 30 * 86400)

        call_count = [0]

        async def fake_refresh(refresh_token):
            call_count[0] += 1
            await asyncio.sleep(0.05)  # 模拟网络延迟,强化竞态
            return {
                "access_token": f"new_a_{call_count[0]}",
                "refresh_token": "new_r",
                "expires_in": 7200,
                "refresh_token_expires_in": 30 * 86400,
            }

        async def go() -> list[str]:
            with patch.object(self.oauth, "_refresh", side_effect=fake_refresh):
                results = await asyncio.gather(*[
                    self.oauth.get_valid_token("ou_race") for _ in range(5)
                ])
            return results

        results = asyncio.run(go())
        # 所有协程拿到同一个 access_token(第一个赢家),后面的走双检直接返回缓存
        self.assertEqual(call_count[0], 1, "should only refresh once under concurrency")
        self.assertTrue(all(r == results[0] for r in results), results)

    # ---------- 小细节 ----------

    def test_token_suffix_never_leaks_full(self) -> None:
        suf = self.oauth.token_suffix("sk-abcdefghijklmn")
        self.assertEqual(suf, "...ijklmn")
        self.assertNotIn("abcde", suf)
        self.assertEqual(self.oauth.token_suffix(""), "<empty>")

    def test_normalize_token_payload_field_aliases(self) -> None:
        # 现代字段
        a, r, ae, re_ = self.oauth._normalize_token_payload({
            "access_token": "a", "refresh_token": "r",
            "expires_in": 3600, "refresh_token_expires_in": 60,
        })
        now = int(time.time())
        self.assertEqual((a, r), ("a", "r"))
        self.assertAlmostEqual(ae, now + 3600, delta=2)
        self.assertAlmostEqual(re_, now + 60, delta=2)

        # 老字段别名
        a, r, ae, re_ = self.oauth._normalize_token_payload({
            "access_token": "a", "refresh_token": "r",
            "expire": 100, "refresh_expire": 200,
        })
        self.assertAlmostEqual(ae, now + 100, delta=2)
        self.assertAlmostEqual(re_, now + 200, delta=2)


if __name__ == "__main__":
    unittest.main()
