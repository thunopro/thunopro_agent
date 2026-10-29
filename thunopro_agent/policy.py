"""Model admission policy: which models a role may use, derived from verified data.

Nothing here assumes a quota. Unlimited status comes only from `quota_tier == 4`
in `.thunopro/models.json`, and that tier must be set after checking the plan
(see docs/MODEL_ROUTING.md). The Go plan limits below are copied from the
OpenCode Go documentation and are informational only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Source: https://opencode.ai/docs/go (checked 2026-10-29), Go base plan, "Estimated requests".
# Tuple = (requests per 5 hours, per week, per month). None = documented as Unlimited.
GO_PLAN_REQUEST_ESTIMATES: dict[str, tuple[int | None, int | None, int | None]] = {
    "opencode-go/beam-free": (None, None, None),
    "opencode-go/iceman-contributor-free": (None, None, None),
    "opencode-go/glm-5.3-flash": (6320, 15790, 31580),
    "opencode-go/glm-5.3": (220, 540, 1080),
    "opencode-go/glm-5.2": (880, 2150, 4300),
    "opencode-go/kimi-k3.1-pro": (147, 368, 735),
    "opencode-go/kimi-k3": (110, 250, 490),
    "opencode-go/kimi-k2.7-code": (1350, 3380, 6750),
    "opencode-go/longcat-2.0": (11400, 28600, 57200),
    "opencode-go/step-5-preview": (430, 1070, 2130),
    "opencode-go/mimo-v2.6-flash": (30100, 75200, 150400),
    "opencode-go/mimo-v2.6-pro": (3250, 8150, 16300),
    "opencode-go/mimo-v2.5": (30100, 75200, 150400),
    "opencode-go/mimo-v2.5-pro": (3250, 8150, 16300),
    "opencode-go/minimax-m3.1-flash": (3880, 9690, 19380),
    "opencode-go/minimax-m3": (3200, 8000, 16000),
    "opencode-go/muse-spark-1.3-contributor": (45300, 113300, 226600),
    "opencode-go/muse-spark-1.2-contributor": (45300, 113300, 226600),
    "opencode-go/qwen3.8-max": (160, 400, 810),
    "opencode-go/qwen3.8-flash": (5400, 13500, 27000),
    "opencode-go/qwen3.7-plus": (4300, 10800, 21600),
    "opencode-go/deepseek-v4.1-flash": (34670, 86670, 173330),
    "opencode-go/deepseek-v4-pro": (1050, 2600, 5200),
    "opencode-go/hy4-preview": (1350, 3380, 6770),
    "opencode-go/hy3": (4300, 10750, 21500),
    "opencode-go/grok-4.7": (169, 423, 845),
    "opencode-go/gpt-6-luna": (4230, 10560, 21130),
    "opencode-go/gpt-5.6-luna": (2050, 5100, 10250),
    "opencode-go/claude-haiku-5-5": (3850, 9620, 19230),
    "opencode-go/gemini-4-argon": (440, 1080, 2120),
}

# Documented as using request data for training (Go privacy table, 2026-10-29).
DOCUMENTED_TRAINS_ON_DATA: frozenset[str] = frozenset({
    "opencode-go/iceman-contributor-free",
    "opencode-go/muse-spark-1.3-contributor",
    "opencode-go/muse-spark-1.2-contributor",
})

# Models documented by the OpenCode Go plan as having no request/token limit.
# Used only to seed verified metadata in `init`; users can still override.
DOCUMENTED_UNLIMITED: frozenset[str] = frozenset({
    "opencode-go/beam-free",
})

DEFAULT_ROUTING: dict[str, Any] = {
    # Only verified-unlimited models (quota_tier == 4) may run workers and subagents.
    "worker_mode": "unlimited_only",
    # Providers whose credentials are configured on this machine. `opencode/*` (Zen)
    # models were listed by `opencode models` but no Zen credential is present.
    "allowed_providers": ["opencode-go"],
    # Models documented as training on request data are excluded unless explicitly allowed.
    "allow_models_trained_on_request_data": False,
}


@dataclass(frozen=True)
class RoutingPolicy:
    worker_mode: str
    allowed_providers: tuple[str, ...]
    allow_training_models: bool

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "RoutingPolicy":
        merged = {**DEFAULT_ROUTING, **(raw or {})}
        providers = merged.get("allowed_providers") or DEFAULT_ROUTING["allowed_providers"]
        if merged["worker_mode"] != "unlimited_only":
            raise ValueError("routing.worker_mode must be 'unlimited_only'")
        return cls(
            worker_mode="unlimited_only",
            allowed_providers=tuple(str(p) for p in providers),
            allow_training_models=bool(merged.get("allow_models_trained_on_request_data", False)),
        )

    def provider_allowed(self, model_id: str) -> bool:
        return model_id.split("/", 1)[0] in self.allowed_providers


def request_estimates(model_id: str) -> tuple[int | None, int | None, int | None] | None:
    """Return documented Go request estimates, or None when the model is not in the table."""
    return GO_PLAN_REQUEST_ESTIMATES.get(model_id)


def trains_on_request_data(model_id: str, configured: bool | None = None) -> bool:
    if configured is not None:
        return configured
    return model_id in DOCUMENTED_TRAINS_ON_DATA


def describe(policy: RoutingPolicy, unlimited_ids: Iterable[str]) -> dict[str, Any]:
    return {
        "worker_mode": policy.worker_mode,
        "allowed_providers": list(policy.allowed_providers),
        "allow_models_trained_on_request_data": policy.allow_training_models,
        "unlimited_models": sorted(unlimited_ids),
    }
