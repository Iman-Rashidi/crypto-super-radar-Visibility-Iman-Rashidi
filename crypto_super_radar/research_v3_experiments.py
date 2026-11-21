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
            "settings": dict(
                e,
                v3_research_filter=True,
                risk_fraction=0.00125,
            ),
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
) -> dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(f"dataset not found: {db_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    database_hash = _sha256(db_path)
    runs: list[dict[str, Any]] = []

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
            replay_months=replay_months,
            training_months=training_months,
            validation_months=validation_months,
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
        runs.append(run)

    # B and C intentionally repeat the same deterministic Shadow setup.
    # A mismatch is a reproducibility failure, not another performance observation.
    if runs[1]["metrics"] != runs[2]["metrics"] or runs[1]["by_phase"] != runs[2]["by_phase"]:
        raise RuntimeError(f"{dataset_name}: B/C identical-configuration replay was not reproducible")

    return {
        "dataset": dataset_name,
        "database_file": db_path.name,
        "database_sha256": database_hash,
        "replay_window": {
            "start_utc": replay_start_utc,
            "replay_months": replay_months,
            "training_months": training_months,
            "validation_months": validation_months - training_months,
            "out_of_sample_months": replay_months - validation_months,
        },
        "scenarios": runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Isolated offline A-E research comparison; no exchange order path."
    )
    parser.add_argument("--replay109-db", required=True)
    parser.add_argument("--independent-db", default=None)
    parser.add_argument("--independent-start-utc", default="2025-11-21T16:00:00+00:00")
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
            replay_months=5,
            training_months=1,
            validation_months=1,
        )
    output = {
        "report_version": "V3_RESEARCH_FILTER_A_TO_E_V1",
        "environment": "OFFLINE_VIRTUAL_BLOFIN_DEMO_MODEL",
        "network_order_writes": False,
        "scenario_policy": {
            "A": "Original baseline parameters.",
            "B": "Scenario E settings with V3 Shadow, per-trade risk 0.25%.",
            "C": "Exact B repeatability control; results must match B.",
            "D": "Scenario E settings with opt-in V3 Research Filter, per-trade risk 0.25%.",
            "E": "Scenario E with opt-in V3 Research Filter and 0.125% per-position risk; two-slot nominal portfolio cap 0.25%.",
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
            for scenario in ("B", "C", "D", "E")
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "v3_research_a_to_e.json"
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
