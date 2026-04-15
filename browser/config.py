from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class BrowserSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    browser_service_token: str
    browser_public_base_url: str = ""
    data_dir: str = "/data"
    browser_idle_timeout_seconds: int = 900
    browser_max_session_ttl_seconds: int = 2700

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def browser_profiles_path(self) -> Path:
        return self.data_path / "browser-profiles"

    def ensure_dirs(self) -> None:
        self.browser_profiles_path.mkdir(parents=True, exist_ok=True)


settings = BrowserSettings()  # type: ignore[call-arg]
