"""Secret-safe diagnostic records for failed Demo execution jobs.

Tracebacks are written to the protected Render application log, while only a
reference and bounded sanitized context are copied to the operational journal.
"""
from __future__ import annotations

import os
import re
import traceback as traceback_module
from collections.abc import Mapping
from typing import Any

_SECRET_ENV_MARKERS = (
    "API_KEY", "API_SECRET", "AUTH_TOKEN", "ACCESS_TOKEN", "REFRESH_TOKEN",
    "TOKEN", "SECRET", "PASSPHRASE", "PASSWORD", "PRIVATE_KEY",
)
_BEARER_RE = re.compile(r"(?i)\\b(Bearer\\s+)[A-Za-z0-9._~+/-]+=*")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|token|api_key|api_secret|signature|secret|passphrase)=)[^&\\s]+"
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\\b(api[_-]?key|api[_-]?secret|auth[_-]?token|access[_-]?token|"
    r"refresh[_-]?token|token|secret|passphrase|password|authorization|signature)"
    r"(\\s*[:=]\\s*)(?:['\"])?([^,\\s&'\"]+)"
)


def redact_diagnostic_text(value: Any, *, environ: Mapping[str, str] | None = None) -> str:
    """Redact known environment secrets and credential-shaped text."""
    text = str(value or "")
    source = os.environ if environ is None else environ
    secret_values = {
        str(secret)
        for name, secret in source.items()
        if any(marker in str(name).upper() for marker in _SECRET_ENV_MARKERS)
        and secret is not None
        and len(str(secret)) >= 4
    }
    for secret in sorted(secret_values, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _BEARER_RE.sub(r"\\1[REDACTED]", text)
    text = _QUERY_SECRET_RE.sub(r"\\1[REDACTED]", text)
    text = _ASSIGNMENT_SECRET_RE.sub(r"\\1\\2[REDACTED]", text)
    return text


def build_execution_error_diagnostic(
    exc: BaseException,
    *,
    failed_operation: str,
    instrument_id: str,
    cycle_id: str,
    job_id: str,
    execution_stage: str,
    database_backend: str,
    timestamp_utc: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a bounded, secret-redacted execution failure record."""
    message = redact_diagnostic_text(str(exc), environ=environ)[:1200]
    rendered_traceback = "".join(
        traceback_module.format_exception(type(exc), exc, exc.__traceback__)
    )
    rendered_traceback = redact_diagnostic_text(
        rendered_traceback, environ=environ
    )[:16000]
    safe_job_id = redact_diagnostic_text(job_id, environ=environ)[:128]
    safe_timestamp = redact_diagnostic_text(timestamp_utc, environ=environ)[:64]
    return {
        "error_type": type(exc).__name__[:128],
        "error_message": message,
        "traceback": rendered_traceback,
        "failed_operation": redact_diagnostic_text(failed_operation, environ=environ)[:160],
        "instrument_id": redact_diagnostic_text(instrument_id, environ=environ)[:64],
        "cycle_id": redact_diagnostic_text(cycle_id, environ=environ)[:128],
        "job_id": safe_job_id,
        "execution_stage": redact_diagnostic_text(execution_stage, environ=environ)[:128],
        "database_backend": redact_diagnostic_text(database_backend, environ=environ)[:64],
        "timestamp_utc": safe_timestamp,
        "error_trace_reference": f"render-stdout:{safe_job_id}:{safe_timestamp}",
    }
