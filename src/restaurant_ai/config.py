"""Central configuration.

Every tunable in the platform is declared here and sourced from the environment
(or a local ``.env``). Nothing else in the codebase reads ``os.environ`` directly.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field, field_validator
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
    # What a guest sees, as against what the ledger records. "MYR 24.90" is
    # right in a journal and wrong on a social post; nobody writes the ISO code
    # on a menu. Kept beside the code so the two move together.
    currency_symbol: str = "RM"

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
    llm_provider: Literal["fake", "anthropic", "google", "ollama"] = "fake"
    # Who answers when a person is waiting. Unset means "the same as everything
    # else", which is the right default and what every existing .env means.
    #
    # It exists because the two kinds of call have nothing in common but the
    # API. A scheduled agent run at 06:00 can take three minutes on a local
    # model and cost nothing; the same three minutes on the owner's question at
    # lunchtime is a bot that looks dead. Splitting them lets a machine under
    # the counter do the bulk work for free while the chat stays quick.
    llm_provider_interactive: Literal["fake", "anthropic", "google", "ollama"] | None = None

    # --- Ollama (local models: Hermes, Llama, Qwen, …) ---
    # No key, no quota, no bill, and no network beyond the host itself. The cost
    # is hardware: an 8B model quantised needs roughly 5GB of RAM, and answers
    # in minutes on a CPU rather than seconds.
    ollama_host: str = "http://localhost:11434"
    # `:latest` because that is what a plain `ollama pull hermes3` leaves on the
    # machine. Defaulting to a tag the documented command does not produce is a
    # first run that fails on a model the owner correctly believes they pulled.
    ollama_model_reasoning: str = "hermes3:latest"
    ollama_model_conversational: str = "hermes3:latest"
    # Ollama's own default context is far smaller than one agent's prompt, and
    # an over-long prompt is truncated silently rather than refused — so the
    # agent loses its instructions and never learns that it did.
    ollama_context: int = 16384

    # --- Anthropic ---
    anthropic_api_key: str = ""
    model_reasoning: str = "claude-opus-5"
    model_conversational: str = "claude-sonnet-5"

    # --- Google ---
    # Both tiers are Flash because the Gemini free tier covers Flash and
    # Flash-Lite only. Anyone with billing attached can point the reasoning tier
    # at a Pro model; the free tier cannot.
    #
    # They are deliberately *different* Flash models. The free-tier quota is
    # `GenerateRequestsPerMinutePerProjectPerModel` — five requests a minute,
    # counted per model — so putting both tiers on one id makes the agents
    # queue behind each other for no reason.
    google_api_key: str = ""
    google_model_reasoning: str = "gemini-3.6-flash"
    google_model_conversational: str = "gemini-3.5-flash"
    # Gemini 3 dropped `thinking_budget` for a thinking *level*. Unset leaves
    # the model on its own default, which is the sane starting point.
    # `restaurant-ai models` lists what a key can actually see — model ids move,
    # and the `-latest` aliases are not safe to pin to (one of them resolved to
    # a deprecated model and 404'd).
    google_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    # Thinking output is drawn from this same budget, so it has to cover the
    # reasoning as well as the answer.
    llm_max_tokens: int = 8192
    # Left unset on purpose. Claude Opus 5 and Sonnet 5 removed the sampling
    # parameters and reject a request that carries one, so sending
    # `temperature: 0.0` — the obvious default for an operations system that
    # wants repeatable answers — fails the call outright. Setting this is only
    # correct against a model old enough to accept it.
    #
    # Gemini 3 Flash is the same story: it uses fixed sampling and discards a
    # temperature it is sent, warning once per call while it does so.
    llm_temperature: float | None = None
    # Anthropic only — Gemini spells this differently, see
    # GOOGLE_REASONING_EFFORT above. Adaptive means the model decides how much
    # to think per request rather than being handed a fixed budget. "disabled"
    # turns it off; "off" sends no thinking field at all, which is what a
    # pre-4.6 model needs.
    llm_thinking: Literal["adaptive", "disabled", "off"] = "adaptive"
    # A rate-limited provider is not an error, it is a queue. The Gemini free
    # tier allows 5 requests/min *per model* and asks for waits of up to a
    # minute; the client defaults (6 for Gemini, 2 for Claude) back off for
    # about half that and then give up, which fails the run and loses the work.
    # This platform fires several agents together on the morning and end-of-day
    # schedules, so that is not a rare case.
    llm_max_retries: int = 10
    # A nightly close should wait out a rate limit rather than fail the books,
    # which is what the patient budget above is for. A person watching a chat
    # will not wait minutes for a reply, and cannot tell a slow answer from a
    # dead bot, so anything someone is waiting on gets these instead.
    llm_interactive_max_retries: int = 1
    llm_interactive_timeout: int = 25
    agent_max_tool_iterations: int = 6

    @field_validator("llm_temperature", "google_reasoning_effort", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """``FOO=`` in a .env means "not set", not "the empty string".

        Without this, copying `.env.example` to `.env` — the documented way to
        start — fails validation and takes the whole platform down before it
        does anything, which reads as a broken codebase rather than a blank
        line in a config file.
        """
        return None if value == "" else value

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
    # Echoed back by Telegram in X-Telegram-Bot-Api-Secret-Token on every
    # webhook call. Without it the callback endpoint is a door anyone can walk
    # through: a forged {"callback_query": {"data": "ok:<id>"}} approves a
    # purchase order, and GET /approvals hands out the ids.
    telegram_webhook_secret: str = ""
    # Guards the approval and agent-run REST endpoints. Unset means those
    # endpoints refuse to serve rather than serve openly — the approval gate is
    # the whole safety story, and shipping it unauthenticated by omission is how
    # it silently stops being one.
    approval_api_key: str = ""

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
