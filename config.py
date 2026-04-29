"""集中配置:从环境变量加载,启动时校验关键字段。"""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Claude Code / Anthropic-compatible backend
    anthropic_auth_token: str
    anthropic_base_url: str = "https://ark.cn-beijing.volces.com/api/coding"
    anthropic_model: str = "ark-code-latest"
    anthropic_default_opus_model: str = "ark-code-latest"
    anthropic_default_sonnet_model: str = "ark-code-latest"
    anthropic_default_haiku_model: str = "ark-code-latest"
    api_timeout_ms: str = "3000000"
    claude_code_disable_nonessential_traffic: str = "1"
    glm_vision_model: str = "glm-5v-turbo"
    glm_vision_base_url: str = "https://api.z.ai/api/paas/v4/chat/completions"
    glm_vision_api_key: str = ""

    # 飞书
    feishu_app_id: str
    feishu_app_secret: str
    feishu_encrypt_key: str = ""
    feishu_verification_token: str = ""
    feishu_admin_open_ids: str = ""
    feishu_allowed_open_ids: str = ""

    # 数据
    data_dir: str = "/data"

    # 运行参数
    agent_max_duration_seconds: int = 1800
    schedule_daily_trigger_limit: int = 50
    browser_service_base_url: str = ""
    browser_service_token: str = ""
    browser_approval_timeout_seconds: int = 300
    browser_queue_wait_timeout_seconds: int = 1200
    log_level: str = "INFO"

    @property
    def allowed_open_ids(self) -> List[str]:
        return [x.strip() for x in self.feishu_allowed_open_ids.split(",") if x.strip()]

    @property
    def admin_open_ids(self) -> List[str]:
        return [x.strip() for x in self.feishu_admin_open_ids.split(",") if x.strip()]

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def sandbox_path(self) -> Path:
        return self.data_path / "sandbox"

    @property
    def sessions_path(self) -> Path:
        return self.data_path / "sessions"

    @property
    def sqlite_path(self) -> Path:
        return self.data_path / "feishu-cc.db"

    @property
    def audit_log_path(self) -> Path:
        return self.data_path / "audit.log"

    def ensure_dirs(self) -> None:
        for p in [self.data_path, self.sandbox_path, self.sessions_path]:
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()  # type: ignore[call-arg]
