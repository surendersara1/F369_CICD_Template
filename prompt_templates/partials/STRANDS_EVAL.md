# SOP — Strands Agent Evaluation (Online Eval, Grounding, Quality Gates)

**Version:** 2.0 · **Last-reviewed:** 2026-04-21 · **Status:** Active
**Applies to:** `strands-agents` ≥ 0.1 · `bedrock-agentcore` Evaluations API · boto3 ≥ 1.34 · CloudWatch metrics · Python 3.12+

---

## 1. Purpose

- Codify the two-layer production evaluation loop:
  1. **Online evaluation (every request)** — heuristic scoring (faithfulness, tool selection, completeness) + sampled AgentCore Evaluations API.
  2. **Grounding validation (post-synthesis)** — ensure every number in the answer can be traced back to source data within 5 % tolerance; regenerate otherwise.
- Codify the quality gate: block when composite < 0.3, disclaim when < 0.5, HIGH/MED/LOW confidence projected to the UI.
- Publish evaluation metrics to CloudWatch (`<project>/Evaluations`) for dashboards and alarms.
- Include when the SOW mentions agent evaluation, quality scoring, grounding validation, hallucination prevention, or confidence scoring.

---

## 2. Decision — Monolith vs Micro-Stack

> **This SOP has no architectural split.** Evaluators run in-process inside the agent container; nothing is provisioned here. §3 is the single canonical variant.
>
> CloudWatch metrics, dashboards, and alarms that consume this SOP's output are defined in `LAYER_OBSERVABILITY` and `OPS_ADVANCED_MONITORING`.

§4 Micro-Stack Variant is intentionally omitted.

---

## 3. Canonical Variant

### 3.1 Two-layer eval architecture

```
Two evaluation layers:

  1. Online Evaluation  (runs on every request)
     - Heuristic scoring: faithfulness, tool_selection, completeness
     - AgentCore Evaluations API (sampled 50%): correctness, faithfulness, helpfulness
     - Composite score → confidence level (HIGH / MEDIUM / LOW)
     - Quality gate:
         composite < 0.3  → block response
         composite < 0.5  → attach disclaimer
         composite ≥ 0.5  → pass through

  2. Grounding Validation  (post-synthesis, before return)
     - Extract numbers from synthesis AND source texts
     - Compute grounding score (% of synthesis numbers found in sources within 5% tolerance)
     - If score < threshold → regenerate with explicit grounding instruction
     - Critical for financial decisions on large amounts
```

### 3.2 Online evaluator

```python
"""Online evaluation — heuristic + AgentCore Evaluations API (sampled)."""
import logging, random, re
import boto3

logger   = logging.getLogger(__name__)
_cw      = boto3.client('cloudwatch')
_bedrock = boto3.client('bedrock-agentcore')

PROJECT_NS     = '{project_name}/Evaluations'
SAMPLE_RATE    = 0.5     # 50% of requests get API eval
EVALUATOR_IDS  = ['builtin:correctness', 'builtin:faithfulness', 'builtin:helpfulness']


class OnlineEvaluator:
    """Runs on every agent turn. Returns a dict of scores keyed by metric."""

    def evaluate_response(
        self,
        query: str,
        result: str,
        tool_calls: list[str],
    ) -> dict[str, float]:
        # Layer 1 — fast heuristic scoring (always runs, no cost)
        scores: dict[str, float] = {
            'faithfulness':   self._score_faithfulness(result, tool_calls),
            'tool_selection': self._score_tool_selection(query, tool_calls),
            'completeness':   self._score_completeness(result),
        }
        scores['composite'] = (
            scores['faithfulness']   * 0.4 +
            scores['tool_selection'] * 0.3 +
            scores['completeness']   * 0.3
        )

        # Layer 2 — AgentCore Evaluations API (sampled — charges per call)
        if random.random() < SAMPLE_RATE:
            for eval_id in EVALUATOR_IDS:
                try:
                    resp = _bedrock.evaluate(
                        evaluatorId=eval_id,
                        evaluationInput={
                            'query':    query[:2000],
                            'response': result[:4000],
                        },
                    )
                    score = resp.get('evaluationResult', {}).get('score', 0.5)
                    scores[f'ac_{eval_id.split(":")[1]}'] = float(score)
                except Exception:
                    # Eval is best-effort — don't block the user response
                    logger.warning("AgentCore evaluator %s failed", eval_id, exc_info=True)

        # Publish to CloudWatch (fire-and-forget)
        try:
            _cw.put_metric_data(
                Namespace=PROJECT_NS,
                MetricData=[
                    {'MetricName': f'eval_{k}', 'Value': v, 'Unit': 'None'}
                    for k, v in scores.items() if isinstance(v, (int, float))
                ],
            )
        except Exception:
            pass

        return scores

    # ── Heuristic scorers ───────────────────────────────────────────────

    def _score_faithfulness(self, result: str, tool_calls: list) -> float:
        """Check numbers in response appear in tool outputs."""
        result_numbers = set(re.findall(r'\d+\.?\d*', result))
        if not result_numbers:
            return 1.0  # No numbers to verify — assume faithful
        tool_numbers = set(re.findall(r'\d+\.?\d*', ' '.join(str(t) for t in tool_calls)))
        if not tool_numbers:
            return 0.5  # No tool data — middling score
        return len(result_numbers & tool_numbers) / len(result_numbers)

    def _score_tool_selection(self, query: str, tool_calls: list) -> float:
        # [Claude: customize query-keyword → expected-tool mapping based on SOW]
        return 1.0 if tool_calls else 0.5

    def _score_completeness(self, result: str) -> float:
        """Heuristic — long enough, has a percentage, has a recommendation verb."""
        checks = [
            len(result) > 200,
            bool(re.search(r'\d+\.?\d*%', result)),
            bool(re.search(r'recommend|action|suggest', result, re.I)),
        ]
        return sum(checks) / len(checks)
```

### 3.3 Quality gate

```python
"""Quality gate — block or disclaim low-quality responses, emit confidence for UI."""

eval_scores = evaluator.evaluate_response(query, str(result), tool_calls_log)
composite   = eval_scores.get('composite', 0.5)

if composite < 0.3:
    # BLOCK — response too low quality to show
    result = (
        "Unable to generate a high-confidence response. "
        "Data sources returned limited or inconsistent information."
    )
elif composite < 0.5:
    # DISCLAIM — keep the response, add a warning banner
    result = str(result) + (
        f"\n\n---\nNote: Lower-than-usual confidence ({composite:.0%}). "
        "Verify key figures independently."
    )

# Confidence object for the UI (rendered next to the answer)
confidence = {
    'level': (
        'HIGH'   if composite > 0.75 else
        'MEDIUM' if composite > 0.50 else
        'LOW'
    ),
    'composite_score': round(composite, 2),
}
```

### 3.4 Grounding validator

```python
"""Grounding validation — prevent hallucinated numbers."""
import re
from dataclasses import dataclass, field


@dataclass
class GroundingResult:
    grounding_score:   float = 1.0
    relevance_score:   float = 1.0
    passed:            bool  = True
    ungrounded_numbers: list = field(default_factory=list)


class GroundingValidator:
    """Compare numbers in the synthesis against numbers in the source texts."""

    def __init__(self, threshold: float = 0.7, tolerance: float = 0.05):
        self.threshold = threshold
        self.tolerance = tolerance

    def validate(self, synthesis: str, sources: list[str], query: str) -> GroundingResult:
        result = GroundingResult()

        syn_numbers = set(re.findall(r'[\d,]+\.?\d*', synthesis))
        if not syn_numbers:
            return result  # No numbers in answer → nothing to verify

        source_text = ' '.join(sources)
        src_numbers = set(re.findall(r'[\d,]+\.?\d*', source_text))
        if not src_numbers:
            result.grounding_score = 0.5
            return result

        grounded = 0
        for syn in syn_numbers:
            try:
                syn_val = float(syn.replace(',', ''))
            except ValueError:
                continue
            if any(
                abs(syn_val - float(s.replace(',', ''))) / max(abs(float(s.replace(',', ''))), 1)
                    < self.tolerance
                for s in src_numbers
            ):
                grounded += 1
            else:
                result.ungrounded_numbers.append(syn)

        result.grounding_score = grounded / len(syn_numbers)
        result.passed = result.grounding_score >= self.threshold
        return result


# Usage in supervisor (regenerate-once pattern)
# validator = GroundingValidator()
# grounding = validator.validate(str(result), [observation, reasoning], query)
# if not grounding.passed:
#     # Regenerate with explicit grounding instruction — ONCE, not in a loop
#     result = synthesizer(
#         f"{synthesis_prompt}\n\n"
#         f"IMPORTANT: Only use numbers from the source data above. "
#         f"The following values could not be verified and must NOT appear: "
#         f"{grounding.ungrounded_numbers}"
#     )
```

### 3.5 Gotchas

- **`bedrock-agentcore.evaluate` is billed per call.** At 50 % sampling across 3 evaluators you're paying for 1.5 eval calls per user turn. Cap aggressively; the heuristic scorer catches most failures for free.
- **Heuristic `faithfulness` via regex matches `2024` against `2024`** in source data. This inflates scores when the year appears everywhere. Strip time-formatting tokens from both sides before scoring in high-stakes domains.
- **`GroundingValidator` regenerate-once**, never loop. The LLM can re-hallucinate with different numbers; after one regeneration, disclaim instead of regenerating again.
- **`_cw.put_metric_data` in a hot loop** can throttle at 150 TPS per account; aggregate scores into a single `put_metric_data` batch (as above) rather than one call per metric.
- **Sample rate vs. diagnostics.** At 50 % sampling you only see API-based scores on half the traffic. Run 100 % in the first week of a new agent release; dial back once the signal is stable.
- **Ungrounded number list leaks** into logs. Treat it as PII-adjacent — amounts, dates — and scrub before persisting.
- **Composite weights (0.4 / 0.3 / 0.3)** are a starting point. Tune via offline labeling before shipping changes.

---

## 5. Swap matrix — evaluation variants

| Need | Swap |
|---|---|
| Offline eval only (batch) | Remove online evaluator; run AgentCore Evaluations over a logged-request dataset nightly |
| Zero-cost online eval | Sample rate = 0; keep only heuristic layer |
| Stricter grounding for financial decisions | Raise `threshold` to 0.9, `tolerance` to 0.01 |
| No regeneration (block instead) | If `grounding.passed` is False → return the block-message from quality gate |
| Custom evaluator | Replace `EVALUATOR_IDS` with your own evaluator ARN (`arn:aws:bedrock-agentcore:…:evaluator/…`) |
| Per-tenant confidence thresholds | Store `threshold` / gate cutoffs in DDB tenant config; load per request |
| Domain-specific completeness heuristic | Replace `_score_completeness` checks with domain keywords (e.g. "covenant", "approver") |

---

## 6. Worked example — scorer and validator offline

Save as `tests/sop/test_STRANDS_EVAL.py`. Offline; no boto calls.

```python
"""SOP verification — scorer returns expected ranges; validator catches ungrounded number."""
from unittest.mock import patch
from shared.eval import OnlineEvaluator, GroundingValidator


def test_faithfulness_penalises_invented_numbers():
    with patch('boto3.client'):
        ev = OnlineEvaluator()
    s = ev._score_faithfulness(
        result="Revenue was 42 and profit was 99",
        tool_calls=["revenue=42"],
    )
    assert 0 <= s <= 1.0
    assert s < 1.0  # '99' was never in tool output


def test_grounding_validator_flags_hallucinated_number():
    gv = GroundingValidator(threshold=0.8, tolerance=0.01)
    r = gv.validate(
        synthesis="Revenue was 42.0 and profit was 100.5",
        sources=["revenue = 42.0 (FY24)"],
        query="what were Q4 numbers?",
    )
    assert r.passed is False
    assert "100.5" in r.ungrounded_numbers


def test_grounding_validator_passes_when_all_numbers_match_within_tolerance():
    gv = GroundingValidator(threshold=0.7, tolerance=0.05)
    r = gv.validate(
        synthesis="Revenue was 42.1",
        sources=["revenue = 42.0 (FY24)"],   # within 5 %
        query="Q4 numbers?",
    )
    assert r.passed is True
```

---

## 7. References

- `docs/template_params.md` — `EVAL_SAMPLE_RATE`, `GROUNDING_THRESHOLD`, `GROUNDING_TOLERANCE`, `EVAL_NAMESPACE`
- `docs/Feature_Roadmap.md` — feature IDs `STR-11` (eval), `A-30` (grounding), `OBS-14` (eval metrics)
- AgentCore Evaluations API: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core-evaluations.html
- Related SOPs: `STRANDS_AGENT_CORE` (where the evaluator is invoked), `LAYER_OBSERVABILITY` (CloudWatch namespace, dashboards), `OPS_ADVANCED_MONITORING` (alarms on eval-score drift), `LLMOPS_BEDROCK` (guardrails — complementary but distinct from eval)

---

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| 2.0 | 2026-04-21 | Restructured to 8-section SOP. Declared single-variant (framework-only). Added Gotchas (§3.5) on sampling cost, regenerate-once rule, and CloudWatch throttles. Added Swap matrix (§5) and Worked example (§6). Content preserved from v1.0 real-code rewrite. |
| 1.0 | 2026-03-05 | Initial — two-layer evaluator, quality gate, grounding validator. |
