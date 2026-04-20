"""PR 1 — OAuth 回调路由的集成测试。

覆盖:
  - 缺参数 → 400
  - state 无效 → 400
  - 成功路径 → 200 + token 落库 + IM 主动确认
  - IM 确认失败不阻塞 200
  - token 交换失败 → 502
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "test-token")
os.environ.setdefault("FEISHU_APP_ID", "cli_test")
os.environ.setdefault("FEISHU_APP_SECRET", "test-secret")
os.environ.setdefault("RAILWAY_PUBLIC_DOMAIN", "test.example.com")

from fastapi.testclient import TestClient  # noqa: E402

from config import settings  # noqa: E402


class OAuthCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_data_dir = settings.data_dir
        self._tmp = tempfile.TemporaryDirectory()
        settings.data_dir = self._tmp.name

        import feishu.oauth as oauth
        oauth._schema_ready = False
        oauth._refresh_locks.clear()
        self.oauth = oauth

        from app import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        settings.data_dir = self._orig_data_dir
        self._tmp.cleanup()

    def test_callback_missing_params_returns_400(self) -> None:
        r = self.client.get("/feishu/oauth/callback")
        self.assertEqual(r.status_code, 400)
        self.assertIn("失效", r.text)

    def test_callback_bad_state_returns_400(self) -> None:
        r = self.client.get("/feishu/oauth/callback?code=x&state=bogus")
        self.assertEqual(r.status_code, 400)

    def test_callback_success_stores_token_and_notifies(self) -> None:
        state = self.oauth.create_state("ou_alice")

        async def fake_exchange(code):
            return {
                "access_token": "acc_live",
                "refresh_token": "ref_live",
                "expires_in": 7200,
                "refresh_token_expires_in": 30 * 86400,
            }

        with patch.object(self.oauth, "exchange_code_for_token",
                          side_effect=fake_exchange) as mex, \
             patch("feishu.client.feishu_client.send_text",
                   new_callable=AsyncMock) as msend:
            r = self.client.get(
                f"/feishu/oauth/callback?code=the_code&state={state}"
            )

        self.assertEqual(r.status_code, 200)
        self.assertIn("授权成功", r.text)
        mex.assert_called_once_with("the_code")
        msend.assert_awaited_once()
        # 确认 IM 消息是发给 alice 的
        args, _ = msend.call_args
        self.assertEqual(args[0], "ou_alice")

        row = self.oauth.read_token("ou_alice")
        self.assertEqual(row.access_token, "acc_live")
        self.assertEqual(row.refresh_token, "ref_live")

    def test_callback_im_failure_still_returns_200(self) -> None:
        state = self.oauth.create_state("ou_bob")

        async def fake_exchange(code):
            return {
                "access_token": "a", "refresh_token": "r",
                "expires_in": 100, "refresh_token_expires_in": 200,
            }

        async def im_boom(*args, **kwargs):
            raise RuntimeError("feishu IM down")

        with patch.object(self.oauth, "exchange_code_for_token",
                          side_effect=fake_exchange), \
             patch("feishu.client.feishu_client.send_text",
                   side_effect=im_boom):
            r = self.client.get(
                f"/feishu/oauth/callback?code=x&state={state}"
            )

        self.assertEqual(r.status_code, 200, r.text)
        # token 落库了(DB 写入先于 IM 推送)
        row = self.oauth.read_token("ou_bob")
        self.assertIsNotNone(row)

    def test_callback_exchange_failure_returns_502(self) -> None:
        state = self.oauth.create_state("ou_cindy")

        async def fail_exchange(code):
            raise self.oauth.OAuthExchangeFailed("upstream 500")

        with patch.object(self.oauth, "exchange_code_for_token",
                          side_effect=fail_exchange):
            r = self.client.get(
                f"/feishu/oauth/callback?code=x&state={state}"
            )

        self.assertEqual(r.status_code, 502)
        # state 已被 consume,不应留存
        with self.assertRaises(self.oauth.OAuthStateInvalid):
            self.oauth.consume_state(state)
        # token 不应落库
        self.assertIsNone(self.oauth.read_token("ou_cindy"))


if __name__ == "__main__":
    unittest.main()
