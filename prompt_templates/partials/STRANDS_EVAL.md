# PARTIAL: Strands Agent Eval — Online Evaluation, Grounding Validation, Quality Gates

**Usage:** Include when SOW mentions agent evaluation, quality scoring, grounding validation, hallucination prevention, or confidence scoring.

---

## Evaluation Architecture (from real production)

```
Two evaluation layers:
  1. Online Evaluation (runs on every request):
     - Heuristic scoring: faithfulness, tool_selection, completeness
     - AgentCore Evaluations API (sampled): correctness, faithfulness, helpfulness
     - Composite score → confidence level (HIGH/MEDIUM/LOW)
     - Quality gate: block response if composite < 0.3, add disclaimer if < 0.5

  2. Grounding Validation (post-synthesis):
     - Extract numbers from synthesis and source texts
     - Compute grounding score (% of numbers found in sources)
     - If ungrounded numbers detected → regenerate with explicit grounding instruction
     - Critical for financial decisions on large amounts
```

---

## Online Evaluator — Pass 3 Reference

```python
"""Online evaluation — heuristic + AgentCore Evaluations API."""
import json, logging, os, random, re
import boto3

logger = logging.getLogger(__name__)
_cw = boto3.client('cloudwatch')
_bedrock = boto3.client('bedrock-agentcore')

class OnlineEvaluator:
    def evaluate_response(self, query: str, result: str, tool_calls: list[str]) -> dict:
        # Layer 1: Fast heuristic scoring (always runs)
        scores = {
            'faithfulness': self._score_faithfulness(result, tool_calls),
            'tool_selection': self._score_tool_selection(query, tool_calls),
            'completeness': self._score_completeness(result),
        }
        scores['composite'] = (
            scores['faithfulness'] * 0.4 +
            scores['tool_selection'] * 0.3 +
            scores['completeness'] * 0.3
        )

        # Layer 2: AgentCore Evaluations API (sampled — more expensive)
        if random.random() < 0.5:
            for eval_id in ['builtin:correctness', 'builtin:faithfulness', 'builtin:helpfulness']:
                try:
                    resp = _bedrock.evaluate(
                        evaluatorId=eval_id,
                        evaluationInput={'query': query[:2000], 'response': result[:4000]},
                    )
                    score = resp.get('evaluationResult', {}).get('score', 0.5)
                    scores[f'ac_{eval_id.split(":")[1]}'] = float(score)
                except Exception:
                    pass

        # Publish to CloudWatch
        try:
            _cw.put_metric_data(Namespace='{{project_name}}/Evaluations',
                MetricData=[{'MetricName': f'eval_{k}', 'Value': v, 'Unit': 'None'}
                            for k, v in scores.items() if isinstance(v, (int, float))])
        except Exception: pass

        return scores

    def _score_faithfulness(self, result, tool_calls):
        """Check numbers in response appear in tool outputs."""
        result_numbers = set(re.findall(r'\d+\.?\d*', result))
        if not result_numbers: return 1.0
        tool_numbers = set(re.findall(r'\d+\.?\d*', ' '.join(str(t) for t in tool_calls)))
        if not tool_numbers: return 0.5
        return len(result_numbers & tool_numbers) / len(result_numbers)

    def _score_tool_selection(self, query, tool_calls):
        # [Claude: customize keyword→tool mapping based on SOW]
        return 1.0 if tool_calls else 0.5

    def _score_completeness(self, result):
        checks = [len(result) > 200, bool(re.search(r'\d+\.?\d*%', result)),
                   bool(re.search(r'recommend|action|suggest', result, re.I))]
        return sum(checks) / len(checks)
```

---

## Quality Gate Pattern — Pass 3 Reference

```python
"""Quality gate — block or disclaim low-quality responses."""
eval_scores = evaluator.evaluate_response(query, str(result), tool_calls_log)
composite = eval_scores.get('composite', 0.5)

if composite < 0.3:
    # BLOCK — response too low quality
    result = ("Unable to generate a high-confidence response. "
              "Data sources returned limited or inconsistent information.")
elif composite < 0.5:
    # DISCLAIM — add warning
    result = str(result) + (
        f"\n\n---\nNote: Lower-than-usual confidence ({composite:.0%}). "
        "Verify key figures independently.")

# Build confidence object for UI
confidence = {
    'level': 'HIGH' if composite > 0.75 else 'MEDIUM' if composite > 0.5 else 'LOW',
    'composite_score': round(composite, 2),
}
```

---

## Grounding Validator — Pass 3 Reference

```python
"""Grounding validation — prevent hallucinated numbers."""
import re, logging
from dataclasses import dataclass, field

@dataclass
class GroundingResult:
    grounding_score: float = 1.0
    relevance_score: float = 1.0
    passed: bool = True
    ungrounded_numbers: list = field(default_factory=list)

class GroundingValidator:
    def __init__(self, threshold: float = 0.7):
        self.threshold = threshold

    def validate(self, synthesis: str, sources: list[str], query: str) -> GroundingResult:
        result = GroundingResult()

        # Extract numbers from synthesis
        syn_numbers = set(re.findall(r'[\d,]+\.?\d*', synthesis))
        if not syn_numbers:
            return result  # No numbers to verify

        # Extract numbers from all sources
        source_text = ' '.join(sources)
        src_numbers = set(re.findall(r'[\d,]+\.?\d*', source_text))

        if not src_numbers:
            result.grounding_score = 0.5
            return result

        # Check each synthesis number against sources (5% tolerance)
        grounded = 0
        for syn in syn_numbers:
            syn_val = float(syn.replace(',', ''))
            if any(abs(syn_val - float(s.replace(',', ''))) / max(abs(float(s.replace(',', ''))), 1) < 0.05
                   for s in src_numbers):
                grounded += 1
            else:
                result.ungrounded_numbers.append(syn)

        result.grounding_score = grounded / len(syn_numbers)
        result.passed = result.grounding_score >= self.threshold
        return result

# Usage in supervisor:
# validator = GroundingValidator()
# grounding = validator.validate(str(result), [observation, reasoning], query)
# if not grounding.passed:
#     # Regenerate with explicit grounding instruction
#     result = synthesizer(f"{prompt}\n\nIMPORTANT: Only use numbers from source data. "
#                          f"Ungrounded: {grounding.ungrounded_numbers}")
```
