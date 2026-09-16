from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEGIS_", env_file=".env", extra="ignore")
    database_url: str = "sqlite+aiosqlite:///./aegisops.db"
    source: Literal["replay", "live"] = "replay"
    provider: Literal["fake", "openai"] = "fake"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "configure-your-model"
    llm_api_key: SecretStr = SecretStr("")
    viewer_token: SecretStr = SecretStr("")
    operator_token: SecretStr = SecretStr("")
    demo_token: SecretStr = SecretStr("")
    demo_only: bool = False
    prometheus_url: str = "http://127.0.0.1:9090"
    loki_url: str = "http://127.0.0.1:3100"
    tempo_url: str = "http://127.0.0.1:3200"
    demo_order_url: str = "http://127.0.0.1:8011"
    demo_inventory_url: str = "http://127.0.0.1:8012"
    otlp_endpoint: str = ""
    lease_seconds: float = Field(30, ge=0.2, le=300)
    io_timeout: float = Field(8, ge=0.05, le=120)
    llm_timeout: float = Field(30, ge=0.1, le=180)
    poll_seconds: float = Field(0.2, ge=0.01, le=30)
    retry_base: float = Field(0.25, ge=0, le=30)
    max_attempts: int = Field(3, ge=1, le=5)
    max_tool_calls: int = Field(24, ge=1, le=100)
    max_llm_calls: int = Field(8, ge=1, le=30)
    max_tokens: int = Field(64000, ge=100, le=200000)
    max_wall_seconds: float = Field(180, ge=0.01, le=1800)
    max_context_bytes: int = Field(14000, ge=1024, le=60000)
    max_tool_bytes: int = Field(6000, ge=512, le=32000)
    max_http_bytes: int = Field(262144, ge=2048, le=1048576)
    max_output_tokens: int = Field(1200, ge=100, le=8000)
    approval_seconds: float = Field(600, ge=0.1, le=3600)
    input_cost_per_million: float | None = Field(None, ge=0)
    output_cost_per_million: float | None = Field(None, ge=0)
    fixtures_dir: Path = Path(__file__).parent / "data" / "scenarios"

    @model_validator(mode="after")
    def coherent(self) -> "Settings":
        if self.provider == "openai" and not self.llm_api_key.get_secret_value():
            raise ValueError("AEGIS_LLM_API_KEY is required for the openai provider")
        if self.source == "live" and not self.demo_only:
            raise ValueError("live is scoped to this demo; set AEGIS_DEMO_ONLY=true")
        return self
