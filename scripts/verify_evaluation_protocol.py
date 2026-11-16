"""Verify the immutable evaluation protocol before any model evaluation."""
from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = ROOT / "docs" / "evaluation_protocol.yaml"
DEFAULT_HASH = ROOT / "docs" / "evaluation_protocol.sha256"


class ProtocolIntegrityError(RuntimeError):
    """The protocol bytes do not match the committed SHA-256 digest."""


class ProtocolNotReadyError(RuntimeError):
    """The protocol is intact but its pre-registration is incomplete."""


def verify_protocol_hash(
    protocol_path: str | Path = DEFAULT_PROTOCOL,
    hash_path: str | Path = DEFAULT_HASH,
) -> str:
    protocol_file = Path(protocol_path)
    digest_file = Path(hash_path)
    if not protocol_file.is_file():
        raise ProtocolIntegrityError(f"protocol file not found: {protocol_file}")
    if not digest_file.is_file():
        raise ProtocolIntegrityError(f"protocol hash file not found: {digest_file}")

    expected = digest_file.read_text(encoding="utf-8").strip().split()
    if not expected or not re.fullmatch(r"[0-9a-fA-F]{64}", expected[0]):
        raise ProtocolIntegrityError("registered SHA-256 digest is missing or malformed")

    actual = hashlib.sha256(protocol_file.read_bytes()).hexdigest()
    if actual.lower() != expected[0].lower():
        raise ProtocolIntegrityError(
            f"evaluation protocol SHA-256 mismatch: expected {expected[0]}, got {actual}"
        )
    return actual


def _scalar(content: str, field: str) -> str | None:
    match = re.search(
        rf"(?m)^[ \t]*{re.escape(field)}:[ \t]*([^\s#]+)[ \t]*$",
        content,
    )
    return None if match is None else match.group(1)


def require_evaluation_ready(protocol_path: str | Path = DEFAULT_PROTOCOL) -> None:
    content = Path(protocol_path).read_text(encoding="utf-8")

    if _scalar(content, "protocol_status") != "LOCKED":
        raise ProtocolNotReadyError("protocol_status must be LOCKED before evaluation")
    if (_scalar(content, "evaluation_allowed") or "").lower() != "true":
        raise ProtocolNotReadyError("evaluation_allowed must be true before evaluation")
    if (_scalar(content, "definitions_locked_before_test") or "").lower() != "true":
        raise ProtocolNotReadyError("market-regime definitions must be locked before Test")
    
    registered_at = _scalar(content, "pre_registered_at_utc")
    if registered_at is None or registered_at.lower() in {"null", "~", "tbd", "unset"}:
        raise ProtocolNotReadyError("pre_registered_at_utc must be registered")
    try:
        parsed = datetime.fromisoformat(
            registered_at[:-1] + "+00:00" if registered_at.endswith("Z") else registered_at
        )
    except ValueError as exc:
        raise ProtocolNotReadyError("pre_registered_at_utc must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProtocolNotReadyError("pre_registered_at_utc must include a UTC timezone")

    # Field, lower bound, upper bound, integer-only.
    requirements: dict[str, tuple[float | None, float | None, bool]] = {
        "purge_window_bars": (0, None, True),
        "embargo_window_bars": (0, None, True),
        "label_horizon_bars": (1, None, True),
        "alpha": (0, 1, False),
        "power": (0, 1, False),
        "minimum_effect_balanced_accuracy": (0, None, False),
        "minimum_effect_macro_f1": (0, None, False),
        "minimum_effect_signal_precision": (0, None, False),
        "minimum_total_samples": (1, None, True),
        "minimum_net_return_after_fees_and_slippage": (None, None, False),
        "maximum_drawdown": (0, 1, False),
        "sharpe_confidence_level": (0, 1, False),
        "sharpe_bootstrap_block_length_bars": (1, None, True),
        "minimum_sharpe_lower_bound_improvement_vs_baseline": (0, None, False),
        "trend_adx_min": (0, None, False),
        "range_adx_max": (0, None, False),
        "high_volatility_atr_percentile_min": (0, 100, False),
        "minimum_samples_per_regime": (1, None, True),
    }
    for field, (lower, upper, integer_only) in requirements.items():
        raw = _scalar(content, field)
        if raw is None or raw.lower() in {"null", "~", "tbd", "unset"}:
            raise ProtocolNotReadyError(f"required threshold is not registered: {field}")
        try:
            value = float(raw)
        except ValueError as exc:
            raise ProtocolNotReadyError(f"required threshold is not numeric: {field}") from exc
        if not math.isfinite(value):
            raise ProtocolNotReadyError(f"required threshold is not finite: {field}")
        if lower is not None and (value <= lower if field in {"alpha", "power", "maximum_drawdown", "sharpe_confidence_level"} else value < lower):
            raise ProtocolNotReadyError(f"required threshold is below its valid bound: {field}")
        if upper is not None and (value >= upper if field in {"alpha", "power", "sharpe_confidence_level"} else value > upper):
            raise ProtocolNotReadyError(f"required threshold is above its valid bound: {field}")
        if integer_only and not value.is_integer():
            raise ProtocolNotReadyError(f"required threshold must be an integer: {field}")


def verify_evaluation_protocol(
    protocol_path: str | Path = DEFAULT_PROTOCOL,
    hash_path: str | Path = DEFAULT_HASH,
    *,
    require_ready: bool = False,
) -> str:
    digest = verify_protocol_hash(protocol_path, hash_path)
    if require_ready:
        require_evaluation_ready(protocol_path)
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--hash-file", type=Path, default=DEFAULT_HASH)
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="also require all statistical, risk, regime, and sample thresholds to be registered",
    )
    args = parser.parse_args()
    try:
        digest = verify_evaluation_protocol(
            args.protocol, args.hash_file, require_ready=args.require_ready
        )
    except ProtocolNotReadyError as exc:
        print(f"PROTOCOL_INTEGRITY=PASS EVALUATION=BLOCKED: {exc}", file=sys.stderr)
        return 2
    except ProtocolIntegrityError as exc:
        print(f"PROTOCOL_INTEGRITY=FAIL EVALUATION=BLOCKED: {exc}", file=sys.stderr)
        return 3
    print(f"PROTOCOL_INTEGRITY=PASS SHA256={digest}")
    print("EVALUATION=READY" if args.require_ready else "EVALUATION_READINESS=NOT_CHECKED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
