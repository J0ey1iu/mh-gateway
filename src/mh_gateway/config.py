from __future__ import annotations

from pydantic import BaseModel, Field


class ConfigSchema(BaseModel):
    """Deployment configuration.

    Every field **without a default** must be supplied by the deployer
    (via env vars or a config centre).  Fields with defaults are optional.
    """

    # ── Database ─────────────────────────────────────
    db_path: str = "./sessions.db"
    db_auto_schema: bool = False

    # ── CORS ────────────────────────────────────────
    cors_origins: list[str] = Field(default_factory=list)

    # ── Development Mode (local dev only) ──────────
    dev_mode: bool = False

    # ── Evaluation ──────────────────────────────────
    enable_eval: bool = True
    eval_results_dir: str = "./eval_results"

    # ── Logging ─────────────────────────────────────
    log_level: str = "INFO"

    # ── SSL (remote agent/tool calls) ───────────────
    verify_agent_tool_ssl: bool = False

    # ── Monitoring ──────────────────────────────────
    metrics_enabled: bool = False
    metrics_push_interval: int = 60

    @property
    def database_url(self) -> str:
        return self.db_path
