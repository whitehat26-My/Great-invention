"""Central configuration.

Every tunable in the platform is declared here and sourced from the environment
(or a local ``.env``). Nothing else in the codebase reads ``os.environ`` directly.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["fake", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Identity -----------------------------------------------------------
    restaurant_name: str = "The Great Invention"
    timezone: str = "Asia/Kuala_Lumpur"
    currency: str = "MYR"

    # --- Postgres -----------------------------------------------------------
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_user: str = "restaurant"
    postgres_password: str = "restaurant"
    postgres_db: str = "restaurant_ai"

    # --- Redis / Celery -----------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6380
    redis_db: int = 0
    celery_task_always_eager: bool = False

    # --- LLM ----------------------------------------------------------------
    # "fake" runs the whole platform deterministically with no API key and no
    # network, which is what the test suite and `simulate-day` use.
    llm_provider: Literal["fake", "anthropic"] = "fake"
    anthropic_api_key: str = ""
    model_reasoning: str = "claude-opus-5"
    model_conversational: str = "claude-sonnet-5"
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.0
    agent_max_tool_iterations: int = 6

    # --- Integration providers ---------------------------------------------
    pos_provider: Provider = "fake"
    messaging_provider: Provider = "fake"
    reviews_provider: Provider = "fake"
    supplier_provider: Provider = "fake"
    social_provider: Provider = "fake"
    payroll_provider: Provider = "fake"
    bank_provider: Provider = "fake"

    # --- Webhook signing ----------------------------------------------------
    webhook_secret: str = "dev-webhook-secret-change-me"
    webhook_tolerance_seconds: int = 300

    # --- Human approval -----------------------------------------------------
    approval_channel: Literal["slack", "telegram", "none"] = "none"
    approval_value_threshold: Decimal = Decimal("250.00")
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_approval_channel: str = "#restaurant-approvals"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Operating policy ---------------------------------------------------
    service_level_z: float = Field(
        1.65, description="Safety-stock service level (1.65 == 95% no-stockout)."
    )
    price_change_max_pct: Decimal = Decimal("0.10")
    price_change_cooldown_days: int = 14
    min_gross_margin_pct: Decimal = Decimal("0.55")
    invoice_price_tolerance_pct: Decimal = Decimal("0.02")
    invoice_qty_tolerance_pct: Decimal = Decimal("0.01")
    reengagement_dormant_days: int = 45
    escalate_review_at_or_below: int = 2

    # --- Observability ------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def psycopg_dsn(self) -> str:
        """Driver-less DSN, which is what LangGraph's PostgresSaver expects."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    # Every field has a default or comes from the environment.
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    """Test hook: drop the memoised Settings so env changes take effect."""
    get_settings.cache_clear()
