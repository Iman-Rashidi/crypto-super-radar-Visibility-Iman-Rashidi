"""Lightweight, deterministic online meta-labeler for offline walk-forward tests.

It does not create buy/sell signals. It only vetoes strategy-generated signals.
Training labels require final net PnL after fees and applicable funding.
No executable pickle or external ML dependency is used.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Mapping

FEATURES = (
    "adx_1h", "rsi_15m", "ema200_distance_pct", "funding_rate_pct",
    "spread_pct", "volume_ratio", "utc_hour_sin", "utc_hour_cos",
    "last_trade_1_win", "last_trade_2_win", "last_trade_3_win",
)
SCALES = {
    "adx_1h": 50.0, "rsi_15m": 100.0, "ema200_distance_pct": 5.0,
    "funding_rate_pct": 0.1, "spread_pct": 0.1, "volume_ratio": 3.0,
    "utc_hour_sin": 1.0, "utc_hour_cos": 1.0,
    "last_trade_1_win": 1.0, "last_trade_2_win": 1.0, "last_trade_3_win": 1.0,
}
MODEL_VERSION = "csr-online-meta-sgd-v1"


class MetaBrainError(ValueError):
    pass


def normalize_features(features: Mapping[str, Any]) -> list[float]:
    if not isinstance(features, Mapping):
        raise MetaBrainError("features must be a mapping")
    missing = [name for name in FEATURES if name not in features]
    if missing:
        raise MetaBrainError("missing features: " + ", ".join(missing))
    vector: list[float] = []
    for name in FEATURES:
        raw = features[name]
        if isinstance(raw, bool) or raw is None:
            raise MetaBrainError(f"{name} is missing")
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MetaBrainError(f"{name} is invalid") from exc
        if not math.isfinite(value):
            raise MetaBrainError(f"{name} is not finite")
        if name == "rsi_15m" and not 0 <= value <= 100:
            raise MetaBrainError("rsi_15m must be within 0..100")
        if name.startswith("last_trade_") and value not in (-1, 0, 1):
            raise MetaBrainError(f"{name} must be -1, 0 or 1")
        vector.append(max(-4.0, min(4.0, value / SCALES[name])))
    return vector


class OnlineMetaBrain:
    """Chronological logistic SGD with a 100-trade warm-up and age decay.

    This model is a meta-filter only; it is not an autonomous signal generator.
    A+ scores never raise the 0.25% per-trade risk ceiling.
    """

    def __init__(
        self, *, warmup_trades: int = 100, min_per_class: int = 20,
        veto_below: float = 0.55, a_plus_above: float = 0.75,
        epochs: int = 5, learning_rate: float = 0.03, decay_half_life: int = 800,
        max_examples: int = 5000,
    ) -> None:
        if warmup_trades < 100 or min_per_class < 10:
            raise ValueError("warm-up must be >=100 and min_per_class >=10")
        if not 0.5 <= veto_below < a_plus_above < 1:
            raise ValueError("probability thresholds are invalid")
        if not 1 <= epochs <= 20 or not 0 < learning_rate <= 0.2:
            raise ValueError("SGD parameters are invalid")
        if max_examples < warmup_trades or max_examples > 100_000:
            raise ValueError("max_examples must be between warmup_trades and 100000")
        self.warmup_trades = int(warmup_trades)
        self.min_per_class = int(min_per_class)
        self.veto_below = float(veto_below)
        self.a_plus_above = float(a_plus_above)
        self.epochs = int(epochs)
        self.learning_rate = float(learning_rate)
        self.decay_half_life = max(100, int(decay_half_life))
        self.max_examples = int(max_examples)
        self.examples: list[dict[str, Any]] = []
        self.weights = [0.0] * len(FEATURES)
        self.bias = 0.0
        self.trained = False
        self.trained_at_utc: str | None = None
        self.train_count = 0

    @staticmethod
    def _sigmoid(value: float) -> float:
        value = max(-35.0, min(35.0, value))
        return 1.0 / (1.0 + math.exp(-value))

    def add_finalized_outcome(
        self, *, signal_id: str, instrument_id: str, candle_time_utc: str,
        features: Mapping[str, Any], net_pnl_usdt: Any,
        fees_complete: bool, funding_complete: bool, source: str = "EXECUTED",
    ) -> dict[str, Any]:
        """Store one immutable label only after every modeled cost is final."""
        if fees_complete is not True or funding_complete is not True:
            return {"status": "PENDING_RECONCILIATION", "inserted": False}
        signal = str(signal_id or "").strip()
        instrument = str(instrument_id or "").strip().upper()
        if not signal or not instrument:
            raise MetaBrainError("signal_id and instrument_id are required")
        pnl = float(net_pnl_usdt)
        if not math.isfinite(pnl):
            raise MetaBrainError("net_pnl_usdt must be finite")
        vector = normalize_features(features)
        stamp = datetime.fromisoformat(str(candle_time_utc).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise MetaBrainError("candle_time_utc must include a timezone")
        if any(row["signal_id"] == signal for row in self.examples):
            return {"status": "DUPLICATE", "inserted": False}
        label = 1 if pnl > 0 else 0
        self.examples.append({
            "signal_id": signal, "instrument_id": instrument,
            "candle_time_utc": stamp.astimezone(timezone.utc).isoformat(),
            "features": vector, "net_pnl_usdt": pnl, "label": label,
            "source": str(source).upper(),
        })
        # Keep a bounded chronological training window; age decay is then
        # applied inside this recent sample window during every SGD pass.
        overflow = len(self.examples) - self.max_examples
        if overflow > 0:
            del self.examples[:overflow]
        return {"status": "RECORDED", "inserted": True, "label": label}

    def train(self) -> dict[str, Any]:
        positives = sum(int(row["label"]) for row in self.examples)
        negatives = len(self.examples) - positives
        if len(self.examples) < self.warmup_trades or min(positives, negatives) < self.min_per_class:
            self.trained = False
            return {"status": "MODEL_NOT_READY", "samples": len(self.examples), "wins": positives, "losses": negatives}
        self.weights = [0.0] * len(FEATURES)
        self.bias = 0.0
        total = len(self.examples)
        for _ in range(self.epochs):
            for index, row in enumerate(self.examples):
                age = total - 1 - index
                sample_weight = 0.5 ** (age / self.decay_half_life)
                vector = row["features"]
                label = int(row["label"])
                probability = self._sigmoid(sum(w * x for w, x in zip(self.weights, vector)) + self.bias)
                error = (probability - label) * sample_weight
                self.weights = [
                    weight - self.learning_rate * (error * x + 0.001 * weight)
                    for weight, x in zip(self.weights, vector)
                ]
                self.bias -= self.learning_rate * error
        self.trained = True
        self.train_count += 1
        self.trained_at_utc = datetime.now(timezone.utc).isoformat()
        return {
            "status": "TRAINED", "model_version": MODEL_VERSION,
            "samples": total, "wins": positives, "losses": negatives,
            "training_round": self.train_count, "trained_at_utc": self.trained_at_utc,
        }

    def evaluate(self, features: Mapping[str, Any]) -> dict[str, Any]:
        vector = normalize_features(features)
        if not self.trained:
            return {"status": "MODEL_NOT_READY", "probability": None, "decision": "SHADOW_ONLY", "risk_multiplier": 1.0}
        probability = self._sigmoid(sum(w * x for w, x in zip(self.weights, vector)) + self.bias)
        if probability < self.veto_below:
            decision = "VETO"
        elif probability >= self.a_plus_above:
            decision = "A_PLUS"
        else:
            decision = "APPROVE"
        return {
            "status": "READY", "model_version": MODEL_VERSION,
            "probability": round(probability, 6), "decision": decision,
            "risk_multiplier": 1.0, "risk_ceiling_fraction": 0.0025,
        }

    def feature_importance(self) -> list[dict[str, Any]]:
        return sorted(
            ({"feature": name, "weight": round(weight, 8), "absolute_weight": abs(weight)}
             for name, weight in zip(FEATURES, self.weights)),
            key=lambda row: row["absolute_weight"], reverse=True,
        )

    def model_sha256(self) -> str:
        """Stable hash of the deployable model identity and feature contract."""
        identity = {
            "model_version": MODEL_VERSION,
            "features": list(FEATURES),
            "weights": self.weights,
            "bias": self.bias,
            "trained": self.trained,
            "warmup_trades": self.warmup_trades,
            "min_per_class": self.min_per_class,
            "veto_below": self.veto_below,
            "a_plus_above": self.a_plus_above,
            "epochs": self.epochs,
            "learning_rate": self.learning_rate,
            "decay_half_life": self.decay_half_life,
            "max_examples": self.max_examples,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def save_json(self, path: str | Path) -> None:
        payload = {
            "model_version": MODEL_VERSION, "features": list(FEATURES),
            "weights": self.weights, "bias": self.bias, "trained": self.trained,
            "trained_at_utc": self.trained_at_utc, "train_count": self.train_count,
            "warmup_trades": self.warmup_trades, "min_per_class": self.min_per_class,
            "veto_below": self.veto_below, "a_plus_above": self.a_plus_above,
            "epochs": self.epochs, "learning_rate": self.learning_rate,
            "decay_half_life": self.decay_half_life, "max_examples": self.max_examples,
            "examples": self.examples, "model_sha256": self.model_sha256(),
        }
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary.replace(destination)

    @classmethod
    def load_json(cls, path: str | Path) -> "OnlineMetaBrain":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("model_version") != MODEL_VERSION or tuple(payload.get("features", ())) != FEATURES:
            raise MetaBrainError("model version/feature schema mismatch")
        model = cls(
            warmup_trades=int(payload["warmup_trades"]),
            min_per_class=int(payload["min_per_class"]),
            veto_below=float(payload["veto_below"]),
            a_plus_above=float(payload["a_plus_above"]),
            epochs=int(payload.get("epochs", 5)),
            learning_rate=float(payload.get("learning_rate", 0.03)),
            decay_half_life=int(payload.get("decay_half_life", 800)),
            max_examples=int(payload.get("max_examples", 5000)),
        )
        weights = payload.get("weights")
        if not isinstance(weights, list) or len(weights) != len(FEATURES):
            raise MetaBrainError("stored model weights are invalid")
        model.weights = [float(x) for x in weights]
        if not all(math.isfinite(x) for x in model.weights):
            raise MetaBrainError("stored model weights are non-finite")
        try:
            model.bias = float(payload.get("bias"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise MetaBrainError("stored model bias is invalid") from exc
        if not math.isfinite(model.bias):
            raise MetaBrainError("stored model bias is non-finite")
        model.trained = bool(payload.get("trained"))
        model.trained_at_utc = payload.get("trained_at_utc")
        model.train_count = int(payload.get("train_count") or 0)
        examples = payload.get("examples", [])
        if not isinstance(examples, list) or len(examples) > model.max_examples:
            raise MetaBrainError("stored training window is invalid")
        seen_ids: set[str] = set()
        restored: list[dict[str, Any]] = []
        for row in examples:
            if not isinstance(row, Mapping):
                raise MetaBrainError("stored example is invalid")
            signal_id = str(row.get("signal_id") or "").strip()
            instrument_id = str(row.get("instrument_id") or "").strip().upper()
            features = row.get("features")
            pnl = row.get("net_pnl_usdt")
            label = row.get("label")
            try:
                pnl_value = float(pnl)
                label_value = int(label)
                vector = [float(value) for value in features]
            except (TypeError, ValueError, OverflowError) as exc:
                raise MetaBrainError("stored example values are invalid") from exc
            if (
                not signal_id or signal_id in seen_ids or not instrument_id
                or len(vector) != len(FEATURES)
                or not all(math.isfinite(value) for value in vector)
                or not math.isfinite(pnl_value) or label_value not in (0, 1)
                or label_value != (1 if pnl_value > 0 else 0)
            ):
                raise MetaBrainError("stored example failed integrity validation")
            try:
                stamp = datetime.fromisoformat(str(row["candle_time_utc"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError) as exc:
                raise MetaBrainError("stored example timestamp is invalid") from exc
            if stamp.tzinfo is None:
                raise MetaBrainError("stored example timestamp must include a timezone")
            seen_ids.add(signal_id)
            restored.append({
                "signal_id": signal_id,
                "instrument_id": instrument_id,
                "candle_time_utc": stamp.astimezone(timezone.utc).isoformat(),
                "features": vector,
                "net_pnl_usdt": pnl_value,
                "label": label_value,
                "source": str(row.get("source") or "EXECUTED").upper(),
            })
        # Examples are appended when labels mature, not necessarily in entry-time
        # order. Preserve append order: it is the chronology used for SGD age decay.
        model.examples = restored
        expected_hash = payload.get("model_sha256")
        if expected_hash is not None and expected_hash != model.model_sha256():
            raise MetaBrainError("stored model SHA-256 does not match its contents")
        return model


class ShadowPatternBrain:
    """Separate six-feature learner for V3 research; it never gates V2."""
    FEATURES=("candle_reversal","candle_continuation","structure_reversal","structure_continuation","elliott_alignment","liquidity_sweep")
    def __init__(self,*,warmup_trades:int=20,min_per_class:int=5)->None:
        if warmup_trades<20 or min_per_class<5: raise ValueError("requires >=20 samples and >=5 per class")
        self.warmup_trades=warmup_trades; self.min_per_class=min_per_class; self.examples=[]; self.weights=[0.0]*len(self.FEATURES); self.bias=0.; self.trained=False; self.train_count=0
    def add_finalized_outcome(self,*,signal_id:str,instrument_id:str,candle_time_utc:str,features:Mapping[str,Any],net_pnl_usdt:Any,fees_complete:bool,funding_complete:bool)->dict[str,Any]:
        if fees_complete is not True or funding_complete is not True: return {"status":"PENDING_RECONCILIATION","inserted":False}
        sid=str(signal_id or "").strip(); symbol=str(instrument_id or "").strip().upper()
        if not sid or not symbol: raise MetaBrainError("pattern outcome requires identifiers")
        if any(x["signal_id"]==sid for x in self.examples): return {"status":"DUPLICATE","inserted":False}
        stamp=datetime.fromisoformat(str(candle_time_utc).replace("Z","+00:00"))
        if stamp.tzinfo is None: raise MetaBrainError("pattern timestamp needs timezone")
        pnl=float(net_pnl_usdt)
        if not math.isfinite(pnl): raise MetaBrainError("pattern PnL must be finite")
        vector=[]
        for key in self.FEATURES:
            raw=(features or {}).get(key,0); raw=int(raw) if isinstance(raw,bool) else raw; v=float(raw)
            if not math.isfinite(v) or v not in (0.,1.): raise MetaBrainError(key+" must be binary")
            vector.append(v)
        self.examples.append({"signal_id":sid,"instrument_id":symbol,"candle_time_utc":stamp.astimezone(timezone.utc).isoformat(),"features":vector,"net_pnl_usdt":pnl,"label":1 if pnl>0 else 0})
        return {"status":"RECORDED","inserted":True}
    def train(self)->dict[str,Any]:
        wins=sum(int(x["label"]) for x in self.examples); losses=len(self.examples)-wins
        if len(self.examples)<self.warmup_trades or min(wins,losses)<self.min_per_class:
            self.trained=False; return {"status":"MODEL_NOT_READY","samples":len(self.examples),"wins":wins,"losses":losses}
        self.weights=[0.]*len(self.FEATURES); self.bias=0.
        for _ in range(8):
            for row in self.examples:
                x=row["features"]; z=max(-30.,min(30.,sum(w*v for w,v in zip(self.weights,x))+self.bias)); score=1/(1+math.exp(-z)); err=score-int(row["label"])
                self.weights=[w-.04*(err*v+.002*w) for w,v in zip(self.weights,x)]; self.bias-=.04*err
        self.trained=True; self.train_count+=1
        return {"status":"TRAINED","samples":len(self.examples),"wins":wins,"losses":losses,"round":self.train_count}
    def evaluate(self,features:Mapping[str,Any])->dict[str,Any]:
        if not self.trained: return {"mode":"SHADOW_ONLY","status":"MODEL_NOT_READY","score_0_1":None}
        x=[float((features or {}).get(k,0)) for k in self.FEATURES]; z=max(-30.,min(30.,sum(w*v for w,v in zip(self.weights,x))+self.bias))
        return {"mode":"SHADOW_ONLY","status":"READY","score_0_1":round(1/(1+math.exp(-z)),6),"semantics":"Uncalibrated model score; not a probability or trade instruction."}
    def feature_weights(self)->list[dict[str,Any]]:
        return [{"feature":k,"weight":round(v,8)} for k,v in zip(self.FEATURES,self.weights)]
