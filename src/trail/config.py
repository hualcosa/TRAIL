"""Runtime configuration, read from the environment with a ``TRAIL_`` prefix.

One :class:`Settings` object serves every process — agent, evals, and CLI. The
values that differ per container (``TRAIL_SERVICE_NAME``, ``TRAIL_AGENT_BASE_URL``)
are set in ``docker-compose.yml``; everything else has a working default.

The API key is a :class:`~pydantic.SecretStr` and is never logged or
serialised: ``repr`` and ``str`` render it as ``**********``, and
``model_dump(mode="json")`` emits the same mask. Read it exactly once, at the
call site, via ``settings.llm_api_key.get_secret_value()``.

The model is reached through the OpenAI SDK's Responses API. That is a
deliberate portability choice rather than a vendor one: OpenAI, Fireworks AI,
Together AI, DeepInfra and DeepSeek's own Responses endpoint all speak the same
dialect, so moving between them is ``TRAIL_LLM_BASE_URL`` plus ``TRAIL_MODEL``
and no code change. See ``README.md`` for the cost and compliance reasoning
behind the current default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed configuration. Every field maps to ``TRAIL_<FIELD>``."""

    model_config = SettingsConfigDict(
        env_prefix="TRAIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Model -------------------------------------------------------------
    llm_api_key: SecretStr

    #: Default is OpenAI's cheapest GPT-5.6 tier. Extraction is a reading task,
    #: not a reasoning one, so this starts at the bottom of the range rather
    #: than the top. Descending from a frontier model and measuring what breaks
    #: on the way down is what the eval harness is for, not a shortcut around
    #: it — though no such descent has been run and published from this
    #: repository yet, so this is a choice and not yet a finding.
    model: str = "gpt-5.6-luna"

    #: ``None`` uses OpenAI's endpoint. Point it at another Responses-API host
    #: (Fireworks, Together, DeepInfra, api.deepseek.com) to swap providers
    #: without touching code. Note that third-party hosts serving open weights
    #: may quantize: a metric that moves after such a swap has two candidate
    #: causes, so change one variable at a time.
    llm_base_url: str | None = None

    prompt_version: str = "2026-08-15.2"

    #: ``none`` disables reasoning. Extraction copies what was said; it does not
    #: deliberate, and reasoning tokens here are latency and spend with nothing
    #: to show for them.
    effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "none"

    max_tokens: int = 2048

    # --- Infrastructure ----------------------------------------------------
    database_url: str = "postgresql://trail:trail@postgres:5432/trail"
    agent_base_url: str = "http://agent:8000"

    #: OTLP/HTTP+protobuf, full signal path. Langfuse does not accept OTLP over
    #: gRPC at all, and the HTTP exporter's ``endpoint=`` kwarg does NOT
    #: auto-append ``/v1/traces`` the way the generic ``OTEL_EXPORTER_OTLP_ENDPOINT``
    #: env var does when a collector reads it directly — the path has to be
    #: spelled out here.
    otel_exporter_otlp_endpoint: str = (
        "http://langfuse-web:3000/api/public/otel/v1/traces"
    )

    #: Extra OTLP headers, in the OTel standard `k=v,k=v` form. Langfuse
    #: authenticates ingestion with HTTP Basic over the project's API key pair,
    #: so this carries `Authorization: Basic <b64(pk:sk)>`. Keeping it a generic
    #: header string rather than a `langfuse_api_key` field is what preserves the
    #: claim in telemetry.py: the vendor lives in configuration, not in code.
    otel_exporter_otlp_headers: str = ""

    #: Where a **browser** reaches the Langfuse UI, which is not where this
    #: process reaches Langfuse's OTLP collector. The endpoint above names a
    #: compose service and is resolved inside the compose network; the link
    #: built from this one is clicked from a laptop, where
    #: ``http://langfuse-web:3000`` resolves to nothing. The two look
    #: interchangeable and are not, and the failure is a dead link in a demo
    #: rather than an error in a log. Deployed behind a real hostname, this
    #: becomes that hostname while the OTLP endpoint stays internal.
    langfuse_ui_base_url: str = "http://localhost:3000"

    #: Langfuse scopes every trace URL by project, so the deep link needs the
    #: project id as well as the host. This is knowable at config time only
    #: because the stack is provisioned headlessly with a fixed project id — see
    #: LANGFUSE_INIT_PROJECT_ID in docker-compose.yml. Change one and change both.
    langfuse_project_id: str = "trail"

    service_name: str = "trail-agent"

    # --- Approved collections content ---------------------------------------
    protocol_path: Path = Path("/app/protocol/collections_1_30_dpd.md")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`, constructed once.

    Cached because reading the environment and the ``.env`` file on every
    request is pointless work, and because a single instance means the whole
    process agrees on the prompt version stamped into traces and records.

    Tests that need different values should call ``get_settings.cache_clear()``
    after patching the environment.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment
