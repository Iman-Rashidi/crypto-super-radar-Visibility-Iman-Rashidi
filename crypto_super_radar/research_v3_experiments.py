from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .ai_sandbox import run_sandbox


SCENARIO_E_OVERRIDES = {"SOL-USDT": 2.25, "ETH-USDT": 1.625}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _portfolio_metrics(report: dict[str, Any]) -> dict[str, Any]:
    portfolio = (report.get("quantitative_metrics") or {}).get("portfolio") or {}
    equity = report.get("equity") or {}
    return {
        "closed_trades": portfolio.get("completed_trades"),
        "wins": portfolio.get("wins"),
        "losses": portfolio.get("losses"),
        "win_rate_pct": portfolio.get("net_win_rate_pct"),
        "net_pnl_usdt": equity.get("net_pnl_usdt"),
        "profit_factor": portfolio.get("profit_factor"),
        "profit_factor_status": portfolio.get("profit_factor_status"),
        "max_drawdown_pct": portfolio.get("maximum_drawdown_pct"),
        "expectancy_usdt_per_trade": portfolio.get("expectancy_usdt_per_trade"),
    }


def _phase_metrics(report: dict[str, Any]) -> dict[str, Any]:
    phases = ((report.get("quantitative_metrics") or {}).get("by_phase") or {})
    return {
        name: {
            "closed_trades": (phases.get(name) or {}).get("completed_trades"),
            "wins": (phases.get(name) or {}).get("wins"),
            "losses": (phases.get(name) or {}).get("losses"),
            "net_pnl_usdt": (phases.get(name) or {}).get("total_net_pnl_usdt"),
            "profit_factor": (phases.get(name) or {}).get("profit_factor"),
            "profit_factor_status": (phases.get(name) or {}).get("profit_factor_status"),
            "max_drawdown_pct": (phases.get(name) or {}).get("maximum_drawdown_pct"),
        }
        for name in ("TRAIN", "VALIDATION", "OUT_OF_SAMPLE")
    }


def _dataset_manifest(db_path: Path) -> dict[str, Any]:
    import sqlite3
    expected_symbols = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LINK-USDT")
    required = {
        (symbol, bar, source)
        for symbol in expected_symbols
        for bar, source in (("15m", "LAST"), ("1H", "LAST"), ("4H", "LAST"), ("15m", "MARK"))
    }
    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            """SELECT instrument_id,bar,source,COUNT(*),MIN(ts),MAX(ts)
               FROM replay_candles GROUP BY instrument_id,bar,source
               ORDER BY instrument_id,bar,source"""
        ).fetchall()
        funding = dict(connection.execute(
            "SELECT instrument_id,COUNT(*) FROM replay_funding GROUP BY instrument_id"
        ).fetchall())
        metadata = {row[0] for row in connection.execute(
            "SELECT instrument_id FROM replay_instruments"
        ).fetchall()}
    finally:
        connection.close()
    series = []
    found = set()
    for symbol, bar, source, count, first_ms, last_ms in rows:
        found.add((str(symbol), str(bar), str(source)))
        series.append({
            "symbol": str(symbol), "bar": str(bar), "source": str(source),
            "candle_count": int(count), "first_open_ms": int(first_ms),
            "last_open_ms": int(last_ms),
        })
    missing_series = sorted([list(row) for row in required - found])
    missing_funding = [symbol for symbol in expected_symbols if int(funding.get(symbol, 0)) < 3]
    missing_metadata = [symbol for symbol in expected_symbols if symbol not in metadata]
    return {
        "expected_symbols": list(expected_symbols),
        "covered_symbols": sorted({row["symbol"] for row in series}),
        "series": series,
        "total_candle_rows": sum(row["candle_count"] for row in series),
        "funding_event_counts": {k:int(v) for k,v in funding.items()},
        "instrument_metadata_symbols": sorted(metadata),
        "missing_required_series": missing_series,
        "missing_funding_symbols": missing_funding,
        "missing_instrument_metadata": missing_metadata,
        "complete_five_symbol_coverage": not missing_series and not missing_funding and not missing_metadata,
    }


def _cost_friction_audit_from_baseline(report_path: Path, minimum_ratio: float) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    ledger = report.get("trade_ledger") or []
    evaluated = [row for row in ledger if row.get("gross_to_fee_ratio_at_signal") is not None]
    below = [row for row in evaluated if float(row["gross_to_fee_ratio_at_signal"]) < minimum_ratio]
    pnl = [float(row["net_pnl_usdt"]) for row in below if row.get("net_pnl_usdt") is not None]
    return {
        "source": "completed B baseline ledger, ratio computed at signal time",
        "minimum_gross_to_fee_ratio": minimum_ratio,
        "completed_baseline_trades_with_ratio": len(evaluated),
        "baseline_trades_below_threshold": len(below),
        "below_threshold_wins": sum(value > 0 for value in pnl),
        "below_threshold_losses": sum(value < 0 for value in pnl),
        "below_threshold_net_pnl_usdt": sum(pnl),
        "interpretation": "Retrospective signal-time cohort classification, not counterfactual execution.",
    }


def _effective_phase_months(
    replay_end_utc: str | None, training_months: int, validation_months: int,
) -> tuple[int, int]:
    """Explicit independent holdouts must never fit on their own observations."""
    return (0, 0) if replay_end_utc is not None else (int(training_months), int(validation_months))


def _scenario_specs() -> list[dict[str, Any]]:
    base = {
        "max_holding_bars": 12,
        "tp1_enabled": True,
        "tp1_reward_r_multiple": 1.2,
        "tp1_fraction": 0.5,
        "risk_fraction": 0.0025,
    }
    e = dict(base, stop_atr_multiplier_overrides=SCENARIO_E_OVERRIDES)
    return [
        {"id": "A", "name": "Baseline", "settings": dict(base)},
        {"id": "B", "name": "Scenario E with V3 Shadow", "settings": dict(e)},
        {"id": "C", "name": "Scenario E Shadow repeatability control", "settings": dict(e)},
        {"id": "D", "name": "Scenario E with V3 Research Filter", "settings": dict(e, v3_research_filter=True)},
        {
            "id": "E",
            "name": "Scenario E Research Filter plus independent risk cap",
            "settings": dict(e, v3_research_filter=True, risk_fraction=0.00125),
        },
        {
            "id": "F",
            "name": "Scenario E with Cost-Friction Guard >= 3.0",
            "settings": dict(e, cost_friction_guard=True, minimum_gross_to_fee_ratio=3.0),
        },
        {
            "id": "G",
            "name": "Scenario E with strict closed-4H regime alignment",
            "settings": dict(e, four_hour_regime_alignment=True),
        },
        {
            "id": "H",
            "name": "Scenario E with Cost-Friction Guard >= 4.5",
            "settings": dict(e, cost_friction_guard=True, minimum_gross_to_fee_ratio=4.5),
        },
        {
            "id": "I",
            "name": "Scenario E with Cost-Friction Guard >= 5.5",
            "settings": dict(e, cost_friction_guard=True, minimum_gross_to_fee_ratio=5.5),
        },
    ]


def run_experiment_set(
    *,
    db_path: Path,
    dataset_name: str,
    output_dir: Path,
    replay_start_utc: str | None = None,
    replay_months: int = 6,
    training_months: int = 4,
    validation_months: int = 5,
    replay_end_utc: str | None = None,
) -> dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(f"dataset not found: {db_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    database_hash = _sha256(db_path)
    manifest = _dataset_manifest(db_path)
    if dataset_name == "independent_5m" and not manifest["complete_five_symbol_coverage"]:
        raise RuntimeError(f"independent dataset manifest is incomplete: {manifest}")
    runs: list[dict[str, Any]] = []
    effective_training_months, effective_validation_months = _effective_phase_months(
        replay_end_utc, training_months, validation_months
    )

    for spec in _scenario_specs():
        scenario_id = spec["id"]
        report_path = output_dir / f"{dataset_name}_{scenario_id}_report.json"
        brain_path = output_dir / f"{dataset_name}_{scenario_id}_v2_brain.json"
        settings = dict(spec["settings"])
        report = run_sandbox(
            db_path=str(db_path),
            output_path=str(report_path),
            brain_path=str(brain_path),
            replay_start_utc=replay_start_utc,
            replay_end_utc=replay_end_utc,
            replay_months=replay_months,
            training_months=effective_training_months,
            validation_months=effective_validation_months,
            **settings,
        )
        if report.get("network_order_writes") is not False:
            raise RuntimeError(f"{dataset_name} {scenario_id}: network order writes are not disabled")

        v3 = report.get("pattern_intelligence_v3_shadow") or {}
        brain = v3.get("adaptive_brain") or {}
        diagnostics = report.get("diagnostics") or {}
        run = {
            "scenario": scenario_id,
            "name": spec["name"],
            "settings": settings,
            "metrics": _portfolio_metrics(report),
            "by_phase": _phase_metrics(report),
            "v3": {
                "mode": v3.get("mode"),
                "research_filter_enabled": v3.get("research_filter_enabled", False),
                "trained": brain.get("trained", False),
                "training_examples": brain.get("training_examples", 0),
                "training_closed_trades": brain.get("training_closed_trades", 0),
                "model_version": brain.get("model_version"),
                "training_timestamp": brain.get("training_timestamp"),
                "model_hash": brain.get("model_hash"),
                "feature_count": brain.get("feature_count", 0),
                "pattern_name_feature_count": brain.get("pattern_name_feature_count", 0),
                "actual_entry_vetoes": diagnostics.get("v3_research_filter_entry_vetoes", 0),
                "capacity_slots_released": diagnostics.get("v3_research_filter_capacity_slots_released", 0),
                "replacement_entries": diagnostics.get("v3_replacement_entries", 0),
            },
            "counterfactual": {
                "available": bool(v3.get("counterfactual", {}).get("available", False)),
                "closed_vetoed_trades": len(v3.get("counterfactual", {}).get("rejected_candidate_ledger", [])),
                "replacement_trade_count": len(v3.get("counterfactual", {}).get("replacement_trade_ledger", [])),
                "rejected_candidate_ledger": v3.get("counterfactual", {}).get("rejected_candidate_ledger", []),
                "replacement_trade_ledger": v3.get("counterfactual", {}).get("replacement_trade_ledger", []),
            },
            "price_candle_blackout": report.get("price_candle_blackout_audit"),
            "report_file": report_path.name,
        }
        if scenario_id == "B":
            run["cost_friction_audit"] = _cost_friction_audit_from_baseline(
                report_path, minimum_ratio=3.0
            )
            run["cost_friction_audits"] = {
                str(threshold): _cost_friction_audit_from_baseline(
                    report_path, minimum_ratio=threshold
                )
                for threshold in (3.0, 4.5, 5.5)
            }
        runs.append(run)

    # B and C intentionally repeat the same deterministic Shadow setup.
    # A mismatch is a reproducibility failure, not another performance observation.
    if runs[1]["metrics"] != runs[2]["metrics"] or runs[1]["by_phase"] != runs[2]["by_phase"]:
        raise RuntimeError(f"{dataset_name}: B/C identical-configuration replay was not reproducible")

    return {
        "dataset": dataset_name,
        "database_file": db_path.name,
        "database_sha256": database_hash,
        "data_manifest": manifest,
        "replay_window": {
            "start_utc": replay_start_utc,
            "end_exclusive_utc": replay_end_utc,
            "replay_months": replay_months if replay_end_utc is None else None,
            "training_months": effective_training_months,
            "validation_months": effective_validation_months - effective_training_months,
            "out_of_sample_months": replay_months - effective_validation_months if replay_end_utc is None else None,
            "explicit_independent_holdout": replay_end_utc is not None,
        },
        "scenarios": runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Isolated offline A-E research comparison; no exchange order path."
    )
    parser.add_argument("--replay109-db", required=True)
    parser.add_argument("--independent-db", default=None)
    parser.add_argument("--independent-start-utc", default="2025-10-25T00:00:00+00:00")
    parser.add_argument("--independent-end-utc", default="2026-03-09T00:00:00+00:00")
    parser.add_argument("--output-dir", default="artifacts/v3-research")
    args = parser.parse_args()

    root = Path(args.output_dir)
    replay109 = run_experiment_set(
        db_path=Path(args.replay109_db),
        dataset_name="replay109",
        output_dir=root / "replay109",
    )
    independent = None
    if args.independent_db:
        independent = run_experiment_set(
            db_path=Path(args.independent_db),
            dataset_name="independent_5m",
            output_dir=root / "independent_5m",
            replay_start_utc=args.independent_start_utc,
            replay_end_utc=args.independent_end_utc,
            replay_months=5,
            training_months=0,
            validation_months=0,
        )
    output = {
        "report_version": "V3_RESEARCH_FILTER_A_TO_I_V2",
        "environment": "OFFLINE_VIRTUAL_BLOFIN_DEMO_MODEL",
        "network_order_writes": False,
        "scenario_policy": {
            "A": "Original baseline parameters.",
            "B": "Scenario E settings with V3 Shadow, per-trade risk 0.25%.",
            "C": "Exact B repeatability control; results must match B.",
            "D": "Scenario E settings with opt-in V3 Research Filter, per-trade risk 0.25%.",
            "E": "Scenario E with opt-in V3 Research Filter and 0.125% per-position risk; two-slot nominal portfolio cap 0.25%.",
            "F": "Scenario E with signal-time modeled gross-to-fee ratio >= 3.0.",
            "G": "Scenario E with strict alignment to fully closed 4H regime candles.",
            "H": "Scenario E with signal-time modeled gross-to-fee ratio >= 4.5.",
            "I": "Scenario E with signal-time modeled gross-to-fee ratio >= 5.5.",
        },
        "replay109": replay109,
        "independent_5m": independent,
        "independent_5m_status": "COMPLETED" if independent is not None else "NOT_RUN_INDEPENDENT_DOWNLOAD_UNAVAILABLE",
        "profit_factor_thresholds": {
            scenario: {
                "profit_factor": next(row for row in replay109["scenarios"] if row["scenario"] == scenario)["metrics"]["profit_factor"],
                "status": next(row for row in replay109["scenarios"] if row["scenario"] == scenario)["metrics"]["profit_factor_status"],
                "at_least_1_10": (
                    (next(row for row in replay109["scenarios"] if row["scenario"] == scenario)["metrics"]["profit_factor"] or 0) >= 1.10
                    if next(row for row in replay109["scenarios"] if row["scenario"] == scenario)["metrics"]["profit_factor_status"] == "CALCULATED"
                    else False
                ),
                "at_least_1_25": (
                    (next(row for row in replay109["scenarios"] if row["scenario"] == scenario)["metrics"]["profit_factor"] or 0) >= 1.25
                    if next(row for row in replay109["scenarios"] if row["scenario"] == scenario)["metrics"]["profit_factor_status"] == "CALCULATED"
                    else False
                ),
            }
            for scenario in ("B", "C", "D", "E", "F", "G", "H", "I")
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "v3_research_a_to_i.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "report_file": str(path),
        "replay109_sha256": replay109["database_sha256"],
        "independent_sha256": independent["database_sha256"] if independent is not None else None,
        "replay109_scenarios": replay109["scenarios"],
        "independent_scenarios": independent["scenarios"] if independent is not None else None,
    }, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
