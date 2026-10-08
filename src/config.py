"""Runtime settings, grouped into a single Settings object instead of loose
module-level constants everywhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

SNAPSHOT_DATE = date(2026, 9, 30)  # policy reference date - never date.today()
SECURITY_REVIEW_VALID_DAYS = 365


@dataclass(frozen=True)
class LlmSettings:
    model: str
    base_url: str
    timeout_seconds: float
    max_retries: int
    max_tool_steps: int


@dataclass(frozen=True)
class VendorApiSettings:
    base_url: str
    timeout_seconds: float
    retries: int


class MissingApiKeyError(RuntimeError):
    """No Hugging Face token configured."""


def get_api_key() -> str:
    key = (os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_API_KEY") or "").strip()
    if not key:
        raise MissingApiKeyError(
            "No Hugging Face token found.\n"
            "This project needs an API key to run.\n"
            "  1. Copy .env.example to .env\n"
            "  2. Get a free token at https://huggingface.co/settings/tokens\n"
            "  3. Put it in .env as HF_TOKEN=hf_...\n"
        )
    return key


def has_api_key() -> bool:
    try:
        get_api_key()
        return True
    except MissingApiKeyError:
        return False


def get_model_name() -> str:
    return (os.getenv("MODEL_NAME") or "").strip() or "Qwen/Qwen2.5-72B-Instruct"


def get_base_url() -> str:
    return (os.getenv("LLM_BASE_URL") or "").strip() or "https://router.huggingface.co/v1"


def get_vendor_api_base_url() -> str:
    return (os.getenv("VENDOR_RISK_BASE_URL") or "http://127.0.0.1:8001").rstrip("/")


def llm_settings() -> LlmSettings:
    """Build a snapshot of the LLM settings from the environment."""
    return LlmSettings(
        model=get_model_name(),
        base_url=get_base_url(),
        timeout_seconds=60.0,
        max_retries=2,
        max_tool_steps=8,
    )


def vendor_api_settings() -> VendorApiSettings:
    return VendorApiSettings(base_url=get_vendor_api_base_url(), timeout_seconds=4.0, retries=2)


# Module-level constants kept for call sites that just need one value and
# don't want to build a settings object - both styles point at the same
# numbers.
MAX_TOOL_STEPS = 8
LLM_TIMEOUT_SECONDS = 60.0
LLM_MAX_RETRIES = 2
VENDOR_API_TIMEOUT_SECONDS = 4.0
VENDOR_API_RETRIES = 2
