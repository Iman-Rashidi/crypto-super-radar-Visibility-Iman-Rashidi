"""Verify the immutable evaluation protocol before any model evaluation."""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
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


def require_evaluation_ready(protocol_path: str | Path = DEFAULT_PROTOCOL) -> None:
    content = Path(protocol_path).read_text(encoding="utf-8")
    status = re.search(r"(?m)^protocol_status:\s*([^\s#]+)", content)
    allowed = re.search(r"(?m)^evaluation_allowed:\s*([^\s#]+)", content)
    if status is None or status.group(1) != "LOCKED":
        raise ProtocolNotReadyError("protocol_status must be LOCKED before evaluation")
    if allowed is None or allowed.group(1).lower() != "true":
        raise ProtocolNotReadyError("evaluation_allowed must be true before evaluation")

    required_numeric_fields = (
        "purge_window_bars",
        "embargo_window_bars",
        "label_horizon_bars",
        "alpha",
        "power",
        "minimum_effect_balanced_accuracy",
        "minimum_effect_macro_f1",
        "minimum_effect_signal_precision",
        "minimum_total_samples",
        "minimum_net_return_after_fees_and_slippage",
        "maximum_drawdown",
        "sharpe_confidence_level",
        "minimum_sharpe_lower_bound_improvement_vs_baseline",
        "minimum_samples_per_regime",
    )
    for field in required_numeric_fields:
        match = re.search(rf"(?m)^\s*{re.escape(field)}:\s*([^\s#]+)", content)
        if match is None or match.group(1).lower() in {"null", "~", "tbd", "unset"}:
            raise ProtocolNotReadyError(f"required numeric field is not registered: {field}")
        try:
            value = float(match.group(1))
        except ValueError as exc:
            raise ProtocolNotReadyError(f"required numeric field is not numeric: {field}") from exc
        if not (value == value and abs(value) != float("inf")):
            raise ProtocolNotReadyError(f"required numeric field is not finite: {field}")


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
        help="also require all statistical and risk thresholds to be registered",
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
