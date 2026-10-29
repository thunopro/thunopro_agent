from __future__ import annotations

import math
from collections.abc import Mapping


DIMENSIONS = (
    "requirement_coverage",
    "correctness_tests",
    "independent_review",
    "handoff",
    "efficiency",
    "deduplication",
)
WEIGHTS = dict(zip(DIMENSIONS, (25, 25, 20, 15, 10, 5)))


def _validated_scores(scores: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(scores, Mapping):
        raise TypeError("scores must be a mapping")

    missing = [dimension for dimension in DIMENSIONS if dimension not in scores]
    unknown = [dimension for dimension in scores if dimension not in WEIGHTS]
    if missing:
        raise ValueError(f"Missing required dimensions: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"Unknown dimensions: {', '.join(map(str, unknown))}")

    validated = {}
    for dimension in DIMENSIONS:
        value = scores[dimension]
        if isinstance(value, bool):
            raise TypeError(f"Score for {dimension} must be a real number")
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"Score for {dimension} must be a real number") from exc
        if not math.isfinite(numeric_value):
            raise ValueError(f"Score for {dimension} must be finite")
        if not 0 <= numeric_value <= 5:
            raise ValueError(f"Score for {dimension} must be between 0 and 5")
        validated[dimension] = numeric_value
    return validated


def weighted_score(scores: Mapping[str, float]) -> float:
    """Return the weighted rubric score on a scale from 0 to 5."""
    validated = _validated_scores(scores)
    return math.fsum(validated[dimension] * WEIGHTS[dimension]
                     for dimension in DIMENSIONS) / 100


def _grade(score: float) -> str:
    if score >= 4.5:
        return "excellent"
    if score >= 4:
        return "good"
    if score >= 3:
        return "acceptable"
    if score >= 2:
        return "poor"
    return "critical"


def evaluate(scores: Mapping[str, float],
             evidence: Mapping[str, str] | None = None) -> dict:
    """Evaluate scores and report dimensions without supporting evidence."""
    validated = _validated_scores(scores)
    score = math.fsum(validated[dimension] * WEIGHTS[dimension]
                      for dimension in DIMENSIONS) / 100
    supporting_evidence = evidence or {}
    missing_evidence = [
        dimension for dimension in DIMENSIONS
        if not supporting_evidence.get(dimension, "").strip()
    ]
    return {
        "dimensions": validated,
        "weights": dict(WEIGHTS),
        "score": score,
        "grade": _grade(score),
        "missing_evidence": missing_evidence,
    }
