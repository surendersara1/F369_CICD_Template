"""
Agent Eval Runner — executes test cases against Strands agent and scores results.
Actions: load_dataset, run_test_case, aggregate
"""
import boto3
import os
import json
import time
from decimal import Decimal

s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")
bedrock = boto3.client("bedrock-runtime")
cw = boto3.client("cloudwatch")
ddb = boto3.resource("dynamodb")
results_table = ddb.Table(os.environ["EVAL_RESULTS_TABLE"])

EVAL_NAMESPACE = os.environ.get("EVAL_NAMESPACE", "rag-research-agent/AgentEval")
STAGE = os.environ["STAGE"]
JUDGE_MODEL_ID = os.environ.get("JUDGE_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")


def handler(event, context):
    action = event.get("action", "run_test_case")
    if action == "load_dataset":
        return _load_dataset(event)
    elif action == "run_test_case":
        return _run_test_case(event)
    elif action == "aggregate":
        return _aggregate(event)
    raise ValueError(f"Unknown action: {action}")


def _load_dataset(event: dict) -> dict:
    bucket = os.environ.get("DATASET_BUCKET", event.get("dataset_bucket", ""))
    key = event.get("dataset_key", "golden-datasets/latest.json")
    obj = s3.get_object(Bucket=bucket, Key=key)
    dataset = json.loads(obj["Body"].read())
    return {"dataset_name": dataset.get("dataset_name", "unknown"),
            "version": dataset.get("version", "0.0.0"),
            "test_cases": dataset.get("test_cases", []),
            "total_cases": len(dataset.get("test_cases", []))}


def _run_test_case(event: dict) -> dict:
    eval_run_id = event["eval_run_id"]
    test_case = event["test_case"]
    tc_id = test_case["id"]
    assertions = test_case.get("assertions", {})
    session_id = f"eval-{eval_run_id}-{tc_id}"
    messages = test_case["input"]["messages"]

    start_time = time.time()
    agent_response = ""
    for msg in messages:
        response = lambda_client.invoke(
            FunctionName=os.environ["AGENT_FUNCTION_NAME"],
            InvocationType="RequestResponse",
            Payload=json.dumps({"message": msg["content"], "session_id": session_id, "actor_id": "eval-harness"}))
        payload = json.loads(response["Payload"].read())
        body = json.loads(payload.get("body", "{}"))
        agent_response = body.get("response", "")
    latency_ms = int((time.time() - start_time) * 1000)

    scores = {}
    passed = True
    for phrase in assertions.get("response_contains", []):
        ok = phrase.lower() in agent_response.lower()
        scores[f"contains_{phrase}"] = 1.0 if ok else 0.0
        if not ok: passed = False
    for phrase in assertions.get("response_not_contains", []):
        ok = phrase.lower() not in agent_response.lower()
        scores[f"not_contains_{phrase}"] = 1.0 if ok else 0.0
        if not ok: passed = False
    min_len = assertions.get("min_response_length", 0)
    if min_len > 0:
        scores["min_length"] = 1.0 if len(agent_response) >= min_len else 0.0
        if scores["min_length"] == 0: passed = False
    max_latency = assertions.get("max_latency_ms", 30000)
    scores["latency"] = 1.0 if latency_ms <= max_latency else 0.0
    if scores["latency"] == 0: passed = False

    judge_score, judge_reasoning = None, ""
    judge_config = assertions.get("llm_judge", {})
    if judge_config.get("enabled", False):
        judge_score, judge_reasoning = _llm_judge(
            test_case["description"], messages[-1]["content"], agent_response, judge_config["criteria"])
        scores["llm_judge"] = judge_score / 5.0
        if judge_score < judge_config.get("min_score", 3): passed = False

    overall = sum(scores.values()) / max(len(scores), 1)
    results_table.put_item(Item={
        "eval_run_id": eval_run_id, "test_case_id": tc_id,
        "dataset_version": event.get("dataset_version", "unknown"),
        "tag": test_case.get("tags", ["untagged"])[0],
        "passed": passed, "overall_score": Decimal(str(round(overall, 4))),
        "scores": json.dumps(scores), "latency_ms": latency_ms,
        "agent_response_preview": agent_response[:500],
        "judge_score": judge_score, "judge_reasoning": judge_reasoning,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ttl": int(time.time()) + (90 * 86400),
    })
    return {"test_case_id": tc_id, "passed": passed, "score": float(overall), "latency_ms": latency_ms}


def _llm_judge(description: str, user_input: str, agent_response: str, criteria: str) -> tuple:
    prompt = f"""You are an expert evaluator. Score the agent's response 1-5.
Test case: {description}
User input: {user_input}
Agent response: {agent_response}
Criteria: {criteria}
Scoring: 5=Excellent, 4=Good, 3=Acceptable, 2=Poor, 1=Fail
Respond in JSON: {{"score": <1-5>, "reasoning": "<brief>"}}"""
    try:
        response = bedrock.invoke_model(modelId=JUDGE_MODEL_ID, body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31", "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}]}))
        result = json.loads(response["body"].read())
        parsed = json.loads(result["content"][0]["text"])
        return parsed.get("score", 3), parsed.get("reasoning", "")
    except Exception as e:
        return 3, f"Judge error: {str(e)}"


def _aggregate(event: dict) -> dict:
    eval_run_id = event["eval_run_id"]
    results = event.get("results", [])
    scores, latencies, passed_count, total_count = [], [], 0, 0
    for r in results:
        payload = r.get("result", {}).get("Payload", r.get("result", r))
        if isinstance(payload, str):
            payload = json.loads(payload)
        scores.append(payload.get("score", 0))
        latencies.append(payload.get("latency_ms", 0))
        if payload.get("passed", False): passed_count += 1
        total_count += 1

    overall_score = sum(scores) / max(len(scores), 1)
    avg_latency = sum(latencies) / max(len(latencies), 1)
    pass_rate = passed_count / max(total_count, 1)

    cw.put_metric_data(Namespace=EVAL_NAMESPACE, MetricData=[
        {"MetricName": "OverallScore", "Value": overall_score, "Unit": "None",
         "Dimensions": [{"Name": "Stage", "Value": STAGE}]},
        {"MetricName": "PassRate", "Value": pass_rate, "Unit": "None",
         "Dimensions": [{"Name": "Stage", "Value": STAGE}]},
        {"MetricName": "AverageLatencyMs", "Value": avg_latency, "Unit": "Milliseconds",
         "Dimensions": [{"Name": "Stage", "Value": STAGE}]},
    ])
    return {"eval_run_id": eval_run_id, "overall_score": overall_score, "pass_rate": pass_rate,
            "avg_latency_ms": avg_latency, "total_cases": total_count,
            "passed_cases": passed_count, "failed_cases": total_count - passed_count}
