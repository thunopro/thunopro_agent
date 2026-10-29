# Model routing

Scope: model selection for `thunopro_agent`. This document describes the
routing contract as implemented in `thunopro_agent/core.py`, plus explicitly
labeled roadmap items. It does not assert any provider quota fact.

## Implementation status legend

- **Implemented**: present in `core.py` and covered by tests.
- **Roadmap**: proposed only; not implemented.

## Verified quota and request/token headroom (Implemented)

- Quota is unverified by default. `initialize()` writes `verified=false`,
  `quota_tier=0`, and no headroom for every model.
- `available_models()` only honors `quota_tier`, `monthly_requests`,
  `remaining_requests`, `monthly_tokens`, and `remaining_tokens` when
  `verified=true`. Otherwise tier is forced to `0` and headroom is `None`.
- No provider quota, request allowance, or token allowance is assumed or
  invented. Set `verified=true` and populate headroom only after checking the
  actual provider/account plan.
- Models with `enabled=false` are skipped; `_disable_model()` persists
  `enabled=false` plus `disabled_reason` after region/policy failures.

Metadata fields (Implemented):
- `quota_tier`: integer `0..4`.
- `quota_note`: provenance/verification note.
- `verified`: boolean.
- `enabled`: boolean.
- `disabled_reason`: string, set on permanent disable.
- `monthly_requests`, `remaining_requests`: verified request headroom.
- `monthly_tokens`, `remaining_tokens`: verified token headroom.
- `capabilities`, `reasoning`, `coding`, `context`, `speed`: routing ratings
  (overridable in config; inferred conservatively for unknown models).

Quota tier definitions:
- `0` unknown
- `1` limited
- `2` standard
- `3` high monthly allowance
- `4` unlimited/generous

## Hard capability and quality gate (Implemented)

1. Capability subset: every required capability for the task kind must be in
   `model.capabilities` (`TASK_REQUIREMENTS`).
2. Quality floor (`quality_floor_for`):
   - `4` for `reasoning`, `review`, `long_context`
   - `3` otherwise
   - raised to `4` when the task context contains
     `complex|critical|production|security|high-risk|distributed|deep`
3. Candidates below the floor are removed; if none remain, routing raises
   `RuntimeError` ("minimum quality floor").

`model_override` is allowed only if it passes the same capability and quality
gates.

## Scoring formula (Implemented)

Allowance:

```
if quota_tier == 4: allowance = 1.0
else:
  headrooms = [min(1.0, remaining/monthly)] for requests and tokens when both present and monthly > 0
  headroom = min(headrooms) if any else 0.0
  allowance = 0.7 * quota_tier / 4 + 0.3 * headroom
```

Score:

```
quality = kind-specific rating (reasoning/coding/context; review and general
          average reasoning+coding; testing uses coding; fast uses speed)
free_bonus = 5.0 if model id suffix is "-free" else 0.0
performance = clamp(AVG(evaluation score) - 3.0, [-2.0, 2.0]) if >= 2 samples else 0.0
score = 100 * allowance + 10 * quality / 5 + 2 * speed / 5 + free_bonus + performance
```

- Verified allowance dominates (`100 * allowance`).
- Token cost is not a routing penalty.
- Free models receive a large bonus that wins close ties but does not override a
  verified high allowance or the quality floor.
- Tie-break: `(-score, -quality, id)`.

## Fallback behavior (Implemented)

- `run_worker_task(task_id, max_attempts=3)` builds an ordered list with the
  selected model first, then the remaining ranked models, sliced to
  `max_attempts`.
- Each attempt is recorded in the `attempts` table.
- On success (exit code 0 and output not starting with `Error:` /
  `Upstream request failed`), the task moves to `awaiting_review`.
- On provider/account policy errors containing
  `requires global regions`, `trains on request data`,
  `not available in your region`, or `model is not available`,
  `_disable_model()` permanently disables that model (`enabled=false` +
  `disabled_reason`) and later tasks skip it.
- If all attempts fail, the task is marked `failed`.

## Outcome feedback (Partially implemented)

Implemented:
- `rank_models()` reads `evaluations JOIN tasks` grouped by `(model, kind)`.
- Uses `AVG(evaluation.score)` with a sample count.
- Applies `performance = clamp(avg - 3.0, [-2.0, 2.0])` only when
  `samples >= 2`; otherwise `0.0`.
- Adjustment is added to the score, not a hard gate.
- `route_reason` logs quota tier/state, quality floor, and feedback/samples.

Not implemented (Roadmap):
- Bayesian shrinkage of success rate.
- Implicit zero-score evaluation when `task_complete(failed=True)` or a task
  fails.
- Feedback range `[-1, 1]` (current range is `[-2, 2]`).

## Route reason contents (Implemented)

`route_reason` includes task kind, required capabilities, candidate counts,
quota tier and state (`verified`/`unknown`), quality floor, feedback value and
sample count, and notes that free models win close ties and token cost is not a
penalty.

## Roadmap (Not implemented)

- `verified_at` timestamp and freshness gating of verified quota.
- `availability` and `regions` fields/gates.
- `cost` field and any cost penalty.
- A stored `free` boolean (free status is currently inferred from the `-free`
  id suffix).
- `request_allowance`/`token_allowance` naming (current names are
  `monthly_requests`/`remaining_requests`/`monthly_tokens`/`remaining_tokens`).
- Relaxing the quality threshold by 1 and retrying when no candidate passes.
- Static-catalog fallback with availability marked unknown plus warning.
- Fallback to configured/static catalog when the live catalog is empty (current
  behavior raises `RuntimeError`).
- Full documented tie-break tuple beyond `(-score, -quality, id)`.
- Capability/quality override checks against availability/region (fields absent).
- Logging availability, verification status, and cost components in
  `route_reason`.
- Bayesian outcome feedback and implicit zero evaluation on failure.
