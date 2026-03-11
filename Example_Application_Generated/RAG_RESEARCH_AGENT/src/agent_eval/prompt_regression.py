"""
Prompt Regression Detector — compares eval scores across prompt versions.
Publishes regression alerts to SNS if score drops > threshold.
"""
import boto3
import os
import json
from boto3.dynamodb.conditions import Key
from decimal import Decimal

ddb = boto3.resource("dynamodb")
sns = boto3.client("sns")
results_table = ddb.Table(os.environ["EVAL_RESULTS_TABLE"])


def check_regression(current_run_id: str, baseline_run_id: str, threshold: float = 0.05) -> dict:
    current = _get_run_results(current_run_id)
    baseline = _get_run_results(baseline_run_id)
    regressions, improvements = [], []

    for tc_id, current_score in current.items():
        baseline_score = baseline.get(tc_id)
        if baseline_score is None:
            continue
        delta = float(current_score) - float(baseline_score)
        entry = {"test_case_id": tc_id, "baseline_score": float(baseline_score),
                 "current_score": float(current_score), "delta": round(delta, 4)}
        if delta < -threshold:
            regressions.append(entry)
        elif delta > threshold:
            improvements.append(entry)

    report = {"current_run": current_run_id, "baseline_run": baseline_run_id,
              "total_compared": len(set(current.keys()) & set(baseline.keys())),
              "regressions": regressions, "improvements": improvements,
              "regression_detected": len(regressions) > 0}

    if regressions:
        alert_arn = os.environ.get("ALERT_TOPIC_ARN")
        if alert_arn:
            sns.publish(TopicArn=alert_arn,
                        Subject=f"Agent Eval Regression — {len(regressions)} test cases",
                        Message=json.dumps(report, indent=2, default=str))
    return report


def _get_run_results(run_id: str) -> dict:
    result = results_table.query(KeyConditionExpression=Key("eval_run_id").eq(run_id))
    return {item["test_case_id"]: item.get("overall_score", Decimal("0"))
            for item in result.get("Items", [])}
