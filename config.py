"""集中配置:从环境变量加载,启动时校验关键字段。"""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GLM
    anthropic_auth_token: str
    anthropic_base_url: str = "https://open.bigmodel.cn/api/anthropic"
    anthropic_default_opus_model: str = "glm-5.1"
    anthropic_default_sonnet_model: str = "glm-5-turbo"
    anthropic_default_haiku_model: str = "glm-4.5-air"
    api_timeout_ms: str = "3000000"
    claude_code_disable_nonessential_traffic: str = "1"

    # 飞书
    feishu_app_id: str
    feishu_app_secret: str
    feishu_encrypt_key: str = ""
    feishu_verification_token: str = ""
    feishu_allowed_open_ids: str = ""

    # 数据
    data_dir: str = "/data"

    # 运行参数
    agent_max_duration_seconds: int = 1800
    schedule_daily_trigger_limit: int = 50
    log_level: str = "INFO"

    @property
    def allowed_open_ids(self) -> List[str]:
        return [x.strip() for x in self.feishu_allowed_open_ids.split(",") if x.strip()]

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
