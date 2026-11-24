"""Evaluates a threshold-rules artifact.

A rules-only change from Data Science (new thresholds, an extra rule on an
existing feature) is a new artifact file: no code change, no new image.
A different model type (e.g. a trained classifier) is a new scorer image
behind the same /score contract.
"""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

_OPS = {"<": Decimal.__lt__, ">": Decimal.__gt__}


class FeatureMismatch(ValueError):
    pass


def load_artifact(models_dir: Path, version: str) -> tuple[dict, str]:
    raw = (models_dir / f"{version}.json").read_bytes()
    artifact = json.loads(raw)
    if artifact["model_version"] != version or artifact["type"] != "threshold_rules":
        raise ValueError(f"artifact {version}.json does not describe threshold_rules {version}")
    for rule in artifact["rules"]:
        if rule["op"] not in _OPS:
            raise ValueError(f"unsupported operator {rule['op']}")
    return artifact, hashlib.sha256(raw).hexdigest()


def score(features: dict, artifact: dict) -> tuple[str, list[str]]:
    if features.get("feature_version") != artifact["feature_version"]:
        raise FeatureMismatch(f"model needs {artifact['feature_version']}, got {features.get('feature_version')}")
    reasons = []
    for rule in artifact["rules"]:
        value = features.get(rule["feature"])
        if value is None:
            raise FeatureMismatch(f"missing feature {rule['feature']}")
        if _OPS[rule["op"]](Decimal(value), Decimal(rule["threshold"])):
            reasons.append(rule["reason"])
    return ("high_risk", reasons) if reasons else ("low_risk", ["within_all_thresholds"])
