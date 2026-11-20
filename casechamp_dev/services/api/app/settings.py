from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_mode: Literal["development", "test", "production"] = "development"
    web_origin: str = "http://localhost:8000"
    database_url: str = "postgresql+psycopg://casechamp@localhost:5432/casechamp"
    migration_database_url: str | None = None
    maintenance_database_url: str | None = None
    session_secret: SecretStr | None = None
    media_root: Path = ROOT / "var/media"
    fixture_root: Path = ROOT / "fixtures"
    contract_root: Path = ROOT / "packages/contracts"
    model_root: Path = ROOT / "var/models"
    ml_mode: Literal["DISABLED", "REPLAY", "LOCAL"] = "DISABLED"
    ml_device: str = "cpu"
    solver_config_version: str = "planner-1.0.0"
    trusted_proxy: str | None = None

    @model_validator(mode="after")
    def production_urls(self):
        if self.app_mode == "production":
            if not self.web_origin.startswith("https://"):
                raise ValueError("WEB_ORIGIN must use HTTPS in production")
            if self.session_secret is None or len(self.session_secret.get_secret_value()) < 32:
                raise ValueError("SESSION_SECRET must contain at least 32 characters in production")
        return self
