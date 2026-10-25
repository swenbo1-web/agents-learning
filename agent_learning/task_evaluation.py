"""Paired external-task evaluation, separate from the authoritative knowledge DB.

No model is selected or called automatically. Explicit commands may run a
trusted argv adapter; its exit code does not imply acceptance. Observations are
imported as JSONL; fingerprints check declarations, not model authenticity.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import signal
import statistics
import subprocess
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

PROTOCOL_VERSION = 1
VARIANTS = ("no_memory", "with_memory")
TOKEN_COMPONENTS = ("worker", "admin", "retrieval", "model_other")
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_RECORDS = 20000
ARTIFACT_UNITS = {"performance": {"ms", "s", "operations/s", "requests/s", "frames/s"},
                  "memory": {"bytes", "KiB", "MiB"},
                  "quality": {"ratio", "percent", "count", "score"}}


class EvaluationError(ValueError):
    """Invalid evaluation input; no runtime database is accessed."""


def fingerprint(value):
    """Stable JSON SHA-256; reject NaN and ambiguous non-JSON inputs."""
    try:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvaluationError("evaluation values must be finite JSON data") from exc
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def file_fingerprint(path):
    """Hash a runner-provided dataset or artifact without importing its contents."""
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _object(value, field):
    if not isinstance(value, dict):
        raise EvaluationError(f"{field} must be an object")
    return value


def _text(value, field, maximum=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise EvaluationError(f"{field} must be nonempty text of at most {maximum} characters")
    return value


def _sha(value, field):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise EvaluationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _integer(value, field, low=0, high=10**12):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise EvaluationError(f"{field} must be an integer from {low} to {high}")
    return value


def _number(value, field):
    if not _finite_number(value) or value < 0:
        raise EvaluationError(f"{field} must be a finite nonnegative number or null")
    return value


def _finite_number(value):
    """JSON integers may exceed float range; never leak conversion overflow."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _finite_sum(values):
    """Keep ordinary sums byte-compatible; an unrepresentable total is unknown."""
    try:
        result = sum(values)
    except OverflowError:
        return None
    return result if _finite_number(result) else None


def _finite_difference(left, right):
    if left is None or right is None:
        return None
    try:
        result = left - right
    except OverflowError:
        return None
    return result if _finite_number(result) else None


def _finite_median(ordered, *, legacy_float=True):
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    pair = ordered[middle - 1:middle + 1]
    if legacy_float:
        # Preserve the existing normal-number JSON shape (e.g. 100.0).
        try:
            result = statistics.median(pair)
        except OverflowError:
            result = None
        if _finite_number(result):
            return result
    # statistics.mean uses exact intermediates, unlike (left + right) / 2.
    try:
        result = statistics.mean(pair)
    except OverflowError:
        return None
    return result if _finite_number(result) else None


def _model(value):
    model = _object(value, "model")
    if set(model) != {"provider", "name", "version", "parameters"}:
        raise EvaluationError("model requires provider, name, version, parameters")
    for key in ("provider", "name", "version"):
        _text(model[key], f"model.{key}", 200)
    _object(model["parameters"], "model.parameters")
    fingerprint(model)
    return model


def _artifact_descriptor(metric):
    _object(metric, "artifact metric")
    identity = _text(metric.get("id"), "artifact metric.id", 100)
    if not identity.isascii() or any(not (char.isalnum() or char in "_.-") for char in identity):
        raise EvaluationError("artifact metric.id must contain ASCII letters, digits, '_', '.' or '-'")
    kind, unit = metric.get("kind"), metric.get("unit")
    if not isinstance(kind, str) or kind not in ARTIFACT_UNITS or not isinstance(unit, str) or unit not in ARTIFACT_UNITS[kind]:
        raise EvaluationError("artifact metric requires a supported kind and explicit unit")
    _text(metric.get("method"), "artifact metric.method", 2000)
    return {key: metric[key] for key in ("id", "kind", "unit", "method")}


def _artifact_value(value, metric):
    if not _finite_number(value):
        raise EvaluationError("artifact metric value must be a finite number or null")
    if metric["unit"] == "score":
        return value
    _number(value, "artifact metric value")
    if metric["unit"] in {"bytes", "count"}:
        _integer(value, "artifact metric value", high=2**63 - 1)
    if metric["unit"] == "ratio" and value > 1 or metric["unit"] == "percent" and value > 100:
        raise EvaluationError("artifact ratio/percent value exceeds its unit range")
    return value


def _artifact_definitions(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise EvaluationError("task.artifact_metrics must contain 1..16 declarations")
    result, identities = [], set()
    for item in items:
        descriptor = _artifact_descriptor(item)
        if set(item) - {*descriptor, "criteria", "criteria_sha256"} or descriptor["id"] in identities:
            raise EvaluationError("artifact metric declaration has duplicate identity or unsupported fields")
        identities.add(descriptor["id"])
        criteria = item.get("criteria")
        if criteria is not None:
            _object(criteria, "artifact metric.criteria")
            operator = criteria.get("operator")
            if set(criteria) != {"operator", "value"} or not isinstance(operator, str) or operator not in {"lt", "lte", "gt", "gte", "eq"}:
                raise EvaluationError("artifact criteria requires operator lt/lte/gt/gte/eq and value")
            _artifact_value(criteria["value"], descriptor)
        criteria_sha = fingerprint(criteria) if criteria is not None else None
        if "criteria_sha256" in item and item["criteria_sha256"] != criteria_sha:
            raise EvaluationError("artifact criteria fingerprint does not match its frozen criteria")
        result.append({**descriptor, "criteria": criteria, "criteria_sha256": criteria_sha})
    return result


def _artifact_observations(items):
    if not isinstance(items, list) or len(items) > 16:
        raise EvaluationError("record.artifact_metrics must be an array of at most 16 observations")
    identities = set()
    for item in items:
        descriptor = _artifact_descriptor(item)
        if set(item) != {*descriptor, "criteria_sha256", "value", "artifact_sha256", "evidence_sha256"} or item["id"] in identities:
            raise EvaluationError("artifact observation requires unique id, descriptor, criteria fingerprint, value and evidence fingerprints")
        identities.add(item["id"])
        for key in ("criteria_sha256", "artifact_sha256", "evidence_sha256"):
            if item[key] is not None:
                _sha(item[key], "artifact metric." + key)
        if item["value"] is not None:
            _artifact_value(item["value"], descriptor)
            if item["artifact_sha256"] is None or item["evidence_sha256"] is None:
                raise EvaluationError("known artifact measurements require artifact and measurement evidence SHA-256")


def create_protocol(spec):
    """Freeze task inputs and a balanced execution order before collecting results.

    Required spec: id, data_class (real_protocol or synthetic_template), model,
    environment_sha256, knowledge_sha256, repeats (2..100), seed, tasks.
    Each task requires id, prompt, dataset_sha256, acceptance_criteria. Optional
    artifact_metrics declares id/kind/unit/method and criteria={operator,value};
    criteria may be null. Its fingerprint and metric conditions are frozen.
    """
    spec = _object(spec, "spec")
    _text(spec.get("id"), "id", 200)
    if not isinstance(spec.get("data_class"), str) or spec["data_class"] not in {"real_protocol", "synthetic_template"}:
        raise EvaluationError("data_class must explicitly be real_protocol or synthetic_template")
    _model(spec.get("model"))
    _sha(spec.get("environment_sha256"), "environment_sha256")
    _sha(spec.get("knowledge_sha256"), "knowledge_sha256")
    repeats = _integer(spec.get("repeats"), "repeats", 2, 100)
    seed = _integer(spec.get("seed"), "seed", 0, 2**32 - 1)
    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 100:
        raise EvaluationError("tasks must contain 1..100 task definitions")
    normalized = []
    ids = set()
    for task in tasks:
        _object(task, "task")
        identity = _text(task.get("id"), "task.id", 200)
        if identity in ids:
            raise EvaluationError("duplicate task id")
        ids.add(identity)
        prompt = _text(task.get("prompt"), "task.prompt", 100000)
        criteria = _text(task.get("acceptance_criteria"), "task.acceptance_criteria", 100000)
        frozen = {"id": identity, "prompt": prompt, "prompt_sha256": fingerprint(prompt),
                  "dataset_sha256": _sha(task.get("dataset_sha256"), "task.dataset_sha256"),
                  "acceptance_criteria": criteria, "acceptance_sha256": fingerprint(criteria)}
        if "artifact_metrics" in task:
            frozen["artifact_metrics"] = _artifact_definitions(task["artifact_metrics"])
        normalized.append(frozen)
    pairs = [(task["id"], repeat) for task in normalized for repeat in range(1, repeats + 1)]
    generator = random.Random(seed)
    firsts = {task["id"]: generator.randrange(2) for task in normalized}
    generator.shuffle(pairs)
    order = [{"task_id": identity, "repeat": repeat, "variant": variant}
             for identity, repeat in pairs
             for variant in (VARIANTS if (firsts[identity] + repeat) % 2 == 0 else tuple(reversed(VARIANTS)))]
    body = {"kind": "agent_learning.task_evaluation", "version": PROTOCOL_VERSION, "id": spec["id"],
            "data_class": spec["data_class"], "model": spec["model"],
            "environment_sha256": spec["environment_sha256"], "knowledge_sha256": spec["knowledge_sha256"],
            "repeats": repeats, "seed": seed, "tasks": normalized, "execution_order": order,
            "cost_scope": "all_worker_admin_retrieval_other_model_calls_including_failed_attempts",
            "isolation_policy": "fresh_independent_workspace_per_run_frozen_knowledge_no_baseline_memory"}
    body = json.loads(json.dumps(body, ensure_ascii=False, allow_nan=False))
    return {**body, "protocol_sha256": fingerprint(body)}


def _protocol(value):
    _object(value, "protocol")
    if (value.get("kind") != "agent_learning.task_evaluation" or type(value.get("version")) is not int
            or value["version"] != PROTOCOL_VERSION):
        raise EvaluationError("unsupported task evaluation protocol")
    spec = {key: value.get(key) for key in ("id", "data_class", "model", "environment_sha256", "knowledge_sha256",
                                         "repeats", "seed", "tasks")}
    canonical = create_protocol(spec)
    if canonical != value:
        raise EvaluationError("protocol fingerprint or frozen contents do not match")
    return canonical


def _conditions(protocol, task, variant):
    result = {"model": protocol["model"], "environment_sha256": protocol["environment_sha256"],
            "prompt_sha256": task["prompt_sha256"], "dataset_sha256": task["dataset_sha256"],
            "acceptance_sha256": task["acceptance_sha256"], "memory_mode": variant,
            "knowledge_sha256": protocol["knowledge_sha256"] if variant == "with_memory" else None}
    if "artifact_metrics" in task:
        result["artifact_metrics_sha256"] = fingerprint(task["artifact_metrics"])
    return result


def record_template(protocol, task_id, repeat, variant):
    """An unmeasured observation template; unknown values must not become zero.

    Fill observations from an external runner and set source to real_external only
    for actual runs. Keep original conditions and use a unique reset workspace.
    This template is intentionally not eligible for a measured comparison.
    """
    protocol = _protocol(protocol)
    return _new_record(protocol, task_id, repeat, variant)


def _new_record(protocol, task_id, repeat, variant):
    """Internal template constructor for an already validated frozen protocol."""
    task = next((item for item in protocol["tasks"] if item["id"] == task_id), None)
    if task is None or not isinstance(variant, str) or variant not in VARIANTS:
        raise EvaluationError("unknown task or variant")
    _integer(repeat, "repeat", 1, protocol["repeats"])
    conditions = _conditions(protocol, task, variant)
    record = {"run_id": str(uuid.uuid4()), "task_id": task_id, "repeat": repeat, "variant": variant,
            "protocol_sha256": protocol["protocol_sha256"], "conditions": conditions,
            "condition_sha256": fingerprint(conditions), "source": "synthetic_template",
            "acceptance": {"method": "automated", "status": "unknown", "evidence_sha256": None},
            "wall_ms": None, "retrieval": {"latencies_ms": None, "hit": None},
            "token_usage": {component: None for component in TOKEN_COMPONENTS}, "cost": None,
            "isolation": {"workspace_id": "replace-with-unique-reset-workspace", "reset_verified": False,
                          "initial_state_sha256": task["dataset_sha256"]},
            "artifacts": []}
    if "artifact_metrics" in task:
        record["artifact_metrics"] = [{**{key: item[key] for key in ("id", "kind", "unit", "method", "criteria_sha256")},
                                       "value": None, "artifact_sha256": None, "evidence_sha256": None}
                                      for item in task["artifact_metrics"]]
    return record


def _record(record):
    _object(record, "record")
    for key in ("run_id", "task_id"):
        _text(record.get(key), key, 200)
    _integer(record.get("repeat"), "repeat", 1, 100)
    if (not isinstance(record.get("variant"), str) or record["variant"] not in VARIANTS
            or not isinstance(record.get("source"), str) or record["source"] not in {"real_external", "synthetic_template"}):
        raise EvaluationError("invalid variant or result source")
    _sha(record.get("protocol_sha256"), "protocol_sha256")
    _sha(record.get("condition_sha256"), "condition_sha256")
    conditions = _object(record.get("conditions"), "conditions")
    _model(conditions.get("model"))
    for key in ("environment_sha256", "prompt_sha256", "dataset_sha256", "acceptance_sha256"):
        _sha(conditions.get(key), f"conditions.{key}")
    if not isinstance(conditions.get("memory_mode"), str) or conditions["memory_mode"] not in VARIANTS:
        raise EvaluationError("invalid conditions.memory_mode")
    if conditions.get("knowledge_sha256") is not None:
        _sha(conditions["knowledge_sha256"], "conditions.knowledge_sha256")
    if "artifact_metrics_sha256" in conditions:
        _sha(conditions["artifact_metrics_sha256"], "conditions.artifact_metrics_sha256")
    acceptance = _object(record.get("acceptance"), "acceptance")
    if (not isinstance(acceptance.get("method"), str) or acceptance["method"] not in {"user", "automated"}
            or not isinstance(acceptance.get("status"), str) or acceptance["status"] not in {"passed", "failed", "unknown"}):
        raise EvaluationError("acceptance requires user/automated method and passed/failed/unknown status")
    if acceptance.get("evidence_sha256") is not None:
        _sha(acceptance["evidence_sha256"], "acceptance.evidence_sha256")
    if acceptance["status"] != "unknown" and acceptance.get("evidence_sha256") is None:
        raise EvaluationError("known acceptance requires an evidence fingerprint")
    if record.get("wall_ms") is not None:
        _number(record["wall_ms"], "wall_ms")
    retrieval = _object(record.get("retrieval"), "retrieval")
    latencies = retrieval.get("latencies_ms")
    if latencies is not None:
        if not isinstance(latencies, list) or len(latencies) > 10000:
            raise EvaluationError("retrieval.latencies_ms must be an array or null")
        for value in latencies:
            _number(value, "retrieval latency")
    if retrieval.get("hit") is not None and not isinstance(retrieval["hit"], bool):
        raise EvaluationError("retrieval.hit must be boolean or null")
    tokens = _object(record.get("token_usage"), "token_usage")
    if set(tokens) != set(TOKEN_COMPONENTS):
        raise EvaluationError("token_usage must explicitly report worker, admin, retrieval, model_other (null if unknown)")
    for component, values in tokens.items():
        if values is None:
            continue
        _object(values, f"token_usage.{component}")
        if set(values) - {"input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"}:
            raise EvaluationError("unsupported token field")
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if key not in values:
                raise EvaluationError("token components must explicitly include input/output/total fields (null if unknown)")
            if values[key] is not None:
                _integer(values[key], f"{component}.{key}")
        if all(values[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens")) and values["input_tokens"] + values["output_tokens"] != values["total_tokens"]:
            raise EvaluationError("token total does not equal input plus output")
        if values.get("cached_input_tokens") is not None:
            _integer(values["cached_input_tokens"], "cached_input_tokens")
            if values["input_tokens"] is not None and values["cached_input_tokens"] > values["input_tokens"]:
                raise EvaluationError("cached input exceeds input tokens")
    cost = record.get("cost")
    if cost is not None:
        _object(cost, "cost")
        _number(cost.get("amount"), "cost.amount")
        currency = cost.get("currency")
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or currency != currency.upper():
            raise EvaluationError("cost.currency must be a three-letter uppercase currency code")
        if not isinstance(cost.get("scope"), str) or cost["scope"] != "all_calls":
            raise EvaluationError("reported cost must cover all calls, including administrator overhead")
    isolation = _object(record.get("isolation"), "isolation")
    _text(isolation.get("workspace_id"), "isolation.workspace_id", 500)
    if not isinstance(isolation.get("reset_verified"), bool):
        raise EvaluationError("isolation.reset_verified must be boolean")
    _sha(isolation.get("initial_state_sha256"), "isolation.initial_state_sha256")
    artifacts = record.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 100:
        raise EvaluationError("artifacts must be an array of at most 100 references")
    for artifact in artifacts:
        _object(artifact, "artifact")
        _text(artifact.get("ref"), "artifact.ref", 2000)
        _sha(artifact.get("sha256"), "artifact.sha256")
    if "artifact_metrics" in record:
        _artifact_observations(record["artifact_metrics"])
    fingerprint(record)
    return record


def _distribution(values):
    ordered = sorted(values)
    result = {"count": len(ordered), "p50": _finite_median(ordered),
            "p95": ordered[max(0, math.ceil(.95 * len(ordered)) - 1)] if ordered else None,
            "sum": _finite_sum(ordered) if ordered else None}
    overflow = [field for field in ("p50", "sum") if ordered and result[field] is None]
    if overflow:
        result["numeric_overflow"] = overflow
    return result


def _arm_summary(rows):
    outcomes = Counter(row["acceptance"]["status"] for row in rows)
    wall = [row["wall_ms"] for row in rows if row["wall_ms"] is not None]
    latencies = [value for row in rows for value in (row["retrieval"].get("latencies_ms") or [])]
    hit_rows = [row for row in rows if row["retrieval"].get("hit") is not None]
    token_components = {}
    for component in TOKEN_COMPONENTS:
        observations = [row["token_usage"][component] for row in rows if row["token_usage"][component] is not None]
        known = [item for item in observations if all(item[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens"))]
        known_totals = [item["total_tokens"] for item in observations if item["total_tokens"] is not None]
        token_components[component] = {"known_runs": len(known), "unknown_runs": len(rows) - len(known),
                                       "reported_total_tokens": sum(known_totals), "known_total_runs": len(known_totals),
                                       "reported_input_tokens": sum(item["input_tokens"] for item in observations if item["input_tokens"] is not None),
                                       "reported_output_tokens": sum(item["output_tokens"] for item in observations if item["output_tokens"] is not None),
                                       "complete_total_tokens": sum(item["total_tokens"] for item in known) if len(known) == len(rows) and rows else None}
    complete_tokens = bool(rows) and all(item["unknown_runs"] == 0 for item in token_components.values())
    currencies = Counter(row["cost"]["currency"] for row in rows if row.get("cost") is not None)
    cost_totals = {currency: _finite_sum(row["cost"]["amount"] for row in rows if row.get("cost") is not None and row["cost"]["currency"] == currency)
                   for currency in currencies}
    overflow_currencies = [currency for currency, amount in cost_totals.items() if amount is None]
    return {"runs": len(rows), "acceptance": {"passed": outcomes["passed"], "failed": outcomes["failed"],
             "unknown": outcomes["unknown"], "success_rate": outcomes["passed"] / len(rows) if rows and not outcomes["unknown"] else None},
            "wall_ms": {**_distribution(wall), "missing_runs": len(rows) - len(wall)},
            "retrieval_latency_ms": {**_distribution(latencies), "missing_runs": sum(row["retrieval"].get("latencies_ms") is None for row in rows)},
            "retrieval_hit": {"known_runs": len(hit_rows), "hits": sum(row["retrieval"]["hit"] for row in hit_rows),
                              "rate": sum(row["retrieval"]["hit"] for row in hit_rows) / len(hit_rows) if hit_rows else None},
            "token_usage": {"components": token_components, "complete": complete_tokens,
                            "total_tokens": sum(item["reported_total_tokens"] for item in token_components.values()) if complete_tokens else None},
            "cost": {"known_runs": sum(currencies.values()), "unknown_runs": len(rows) - sum(currencies.values()),
                     "reported_by_currency": cost_totals,
                     **({"numeric_overflow": sorted(overflow_currencies)} if overflow_currencies else {})}}


def _artifact_report(protocol, paired_rows, all_rows, complete):
    """Artifact measurements stay separate from worker wall time and token costs.

    Declarations freeze kind/unit/method and optional numeric threshold criteria.
    Known values require both measured-artifact and measurement-evidence SHA-256.
    These are runner declarations, not attestation or professional acceptance.
    """
    declarations = {(task["id"], item["id"]): item for task in protocol["tasks"]
                    for item in task.get("artifact_metrics", [])}
    fields = ("id", "kind", "unit", "method", "criteria_sha256")

    def matches(item, declared):
        return all(item[key] == declared[key] for key in fields)

    unmatched = []
    for row in all_rows:
        for item in row.get("artifact_metrics", []):
            declared = declarations.get((row["task_id"], item["id"]))
            if declared is None or not matches(item, declared):
                unmatched.append({"run_id": row["run_id"], "task_id": row["task_id"], "metric_id": item["id"],
                                  "reason": "metric_not_predeclared" if declared is None else "unit_method_or_criteria_mismatch"})
    groups = {variant: [] for variant in VARIANTS}
    for (task_id, identity), declared in sorted(declarations.items()):
        for variant in VARIANTS:
            rows = [row for row in paired_rows if row["task_id"] == task_id and row["variant"] == variant]
            observed, mismatched = [], 0
            for row in rows:
                item = next((item for item in row.get("artifact_metrics", []) if item["id"] == identity), None)
                if item is not None and not matches(item, declared):
                    mismatched += 1
                elif item is not None and item["value"] is not None:
                    observed.append((row["run_id"], item))
            values = [item["value"] for _, item in observed]
            criteria = declared["criteria"]
            passed = 0
            if criteria is not None:
                target = criteria["value"]
                for value in values:
                    passed += {"lt": value < target, "lte": value <= target, "gt": value > target,
                               "gte": value >= target, "eq": value == target}[criteria["operator"]]
            checked = len(values) if criteria is not None else 0
            expected = protocol["repeats"]
            status = ("passed" if passed == expected else "failed") if complete and checked == expected else "unknown"
            ordered = sorted(values)
            median = _finite_median(ordered, legacy_float=False)
            groups[variant].append({"task_id": task_id, "metric_id": identity, "kind": declared["kind"],
                "unit": declared["unit"], "method": declared["method"], "criteria": criteria,
                "criteria_sha256": declared["criteria_sha256"], "expected_runs": expected,
                "observed_runs": len(rows), "known_runs": len(values), "unknown_runs": expected - len(values),
                "mismatched_runs": mismatched, "values": {"count": len(ordered), "p50": median,
                    "p95": ordered[max(0, math.ceil(.95 * len(ordered)) - 1)] if ordered else None},
                "criteria_results": {"passed": passed, "failed": checked - passed, "unknown": expected - checked,
                                     "status": status},
                "evidence": [{"run_id": run_id, "artifact_sha256": item["artifact_sha256"],
                              "evidence_sha256": item["evidence_sha256"]} for run_id, item in sorted(observed)]})
    return {"sample_scope": "compatible_paired_subset", "groups": groups,
            "unmatched_observations": sorted(unmatched, key=lambda item: (item["run_id"], item["metric_id"])),
            "comparisons_inferred": False,
            "limitations": ["Artifact measurements are independent of worker wall time, retrieval latency and token usage.",
                            "Threshold status checks only frozen criteria; missing criteria or measurements never imply passed.",
                            "Units and methods are matched exactly; no conversions or cross-task pooling are inferred.",
                            "Artifact and evidence fingerprints are runner declarations, not verified artifact quality or authenticity."]}


def summarize(protocol, records):
    """Report all attempted outcomes; missing pairs disable full-design comparison."""
    protocol = _protocol(protocol)
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise EvaluationError(f"records must be an array of at most {MAX_RECORDS} observations")
    task_map = {task["id"]: task for task in protocol["tasks"]}
    expected = {(task["id"], repeat) for task in protocol["tasks"] for repeat in range(1, protocol["repeats"] + 1)}
    invalid, duplicates, conflicts, incompatible = [], [], [], []
    by_id, conflicted_ids = {}, set()
    for index, row in enumerate(records, 1):
        try:
            _record(row)
        except EvaluationError as exc:
            invalid.append({"record": index, "reason": str(exc)})
            continue
        identity = row["run_id"]
        if identity in by_id:
            if by_id[identity] == row:
                duplicates.append(identity)
            else:
                conflicted_ids.add(identity)
        else:
            by_id[identity] = row
    conflicts = sorted(conflicted_ids)
    slots = defaultdict(list)
    workspaces = Counter(row["isolation"]["workspace_id"] for row in by_id.values())
    for identity, row in sorted(by_id.items()):
        reasons = []
        pair = (row["task_id"], row["repeat"])
        if identity in conflicted_ids:
            reasons.append("run_id_reused_with_different_content")
        if pair not in expected:
            reasons.append("outside_predeclared_task_or_repeat")
        if row["protocol_sha256"] != protocol["protocol_sha256"]:
            reasons.append("protocol_fingerprint_mismatch")
        if fingerprint(row["conditions"]) != row["condition_sha256"]:
            reasons.append("condition_fingerprint_mismatch")
        task = task_map.get(row["task_id"])
        if task and row["conditions"] != _conditions(protocol, task, row["variant"]):
            reasons.append("model_environment_prompt_dataset_acceptance_or_memory_mismatch")
        if not row["isolation"]["reset_verified"]:
            reasons.append("workspace_reset_not_verified")
        if task and row["isolation"]["initial_state_sha256"] != task["dataset_sha256"]:
            reasons.append("initial_workspace_state_mismatch")
        if workspaces[row["isolation"]["workspace_id"]] > 1:
            reasons.append("workspace_reused_across_runs")
        if row["source"] != "real_external" or protocol["data_class"] != "real_protocol":
            reasons.append("synthetic_or_template_input")
        if reasons:
            incompatible.append({"run_id": identity, "task_id": pair[0], "repeat": pair[1], "variant": row["variant"], "reasons": reasons})
        slots[(*pair, row["variant"])].append((row, reasons))
    pairs, missing, duplicate_slots, subset = [], [], [], []
    for task_id, repeat in sorted(expected):
        entries = {variant: slots.get((task_id, repeat, variant), []) for variant in VARIANTS}
        missing_variants = [variant for variant, values in entries.items() if not values]
        if missing_variants:
            missing.append({"task_id": task_id, "repeat": repeat, "missing_variants": missing_variants})
        repeated = [variant for variant, values in entries.items() if len(values) > 1]
        if repeated:
            duplicate_slots.append({"task_id": task_id, "repeat": repeat, "variants": repeated})
        pair_ok = not missing_variants and not repeated and all(not values[0][1] for values in entries.values())
        if pair_ok:
            left, right = (entries[variant][0][0] for variant in VARIANTS)
            if left["acceptance"]["method"] != right["acceptance"]["method"]:
                pair_ok = False
                incompatible.append({"task_id": task_id, "repeat": repeat, "reasons": ["acceptance_method_mismatch"]})
            else:
                subset.extend((left, right))
        pairs.append({"task_id": task_id, "repeat": repeat, "compatible": pair_ok,
                      "run_ids": {variant: [item[0]["run_id"] for item in values] for variant, values in entries.items()}})
    arms = {variant: _arm_summary([row for row in subset if row["variant"] == variant]) for variant in VARIANTS}
    complete = all(pair["compatible"] for pair in pairs) and not invalid and not conflicts and not incompatible
    def eligible(available, reason):
        return {"eligible": complete and available, "reason": "complete_predeclared_design" if complete and available else "incomplete_or_incompatible_design" if not complete else reason}
    currencies = {row["cost"]["currency"] for row in subset if row.get("cost") is not None}
    eligibility = {
        "success_rate": eligible(all(arm["acceptance"]["unknown"] == 0 for arm in arms.values()), "acceptance_unknown"),
        "wall_latency": eligible(all(arm["wall_ms"]["missing_runs"] == 0 and arm["wall_ms"]["sum"] is not None for arm in arms.values()),
                                 "wall_latency_numeric_overflow" if any(arm["wall_ms"].get("numeric_overflow") for arm in arms.values()) else "wall_latency_unknown"),
        "total_tokens": eligible(all(arm["token_usage"]["complete"] for arm in arms.values()), "worker_or_overhead_tokens_unknown"),
        "monetary_cost": eligible(all(arm["cost"]["unknown_runs"] == 0 and not arm["cost"].get("numeric_overflow") for arm in arms.values()) and len(currencies) == 1,
                                  "cost_numeric_overflow" if any(arm["cost"].get("numeric_overflow") for arm in arms.values()) else "cost_unknown_or_currency_mismatch"),
        "retrieval_latency": eligible(all(arm["retrieval_latency_ms"]["missing_runs"] == 0 and arm["retrieval_latency_ms"]["count"] > 0
                                          and not arm["retrieval_latency_ms"].get("numeric_overflow") for arm in arms.values()),
                                      "retrieval_latency_numeric_overflow" if any(arm["retrieval_latency_ms"].get("numeric_overflow") for arm in arms.values()) else "retrieval_latency_unknown_or_no_queries")}
    deltas = {"success_rate": None, "wall_ms_total": None, "total_tokens": None, "monetary_cost": None}
    baseline, memory = (arms[variant] for variant in VARIANTS)
    if eligibility["success_rate"]["eligible"]:
        deltas["success_rate"] = _finite_difference(memory["acceptance"]["success_rate"], baseline["acceptance"]["success_rate"])
    if eligibility["wall_latency"]["eligible"]:
        deltas["wall_ms_total"] = _finite_difference(memory["wall_ms"]["sum"], baseline["wall_ms"]["sum"])
    if eligibility["total_tokens"]["eligible"]:
        deltas["total_tokens"] = _finite_difference(memory["token_usage"]["total_tokens"], baseline["token_usage"]["total_tokens"])
    if eligibility["monetary_cost"]["eligible"]:
        currency = next(iter(currencies))
        deltas["monetary_cost"] = {"currency": currency, "amount": _finite_difference(
            memory["cost"]["reported_by_currency"][currency], baseline["cost"]["reported_by_currency"][currency])}
    report = {"kind": "agent_learning.task_evaluation_report", "version": PROTOCOL_VERSION,
            "protocol_id": protocol["id"], "protocol_sha256": protocol["protocol_sha256"],
            "expected_pairs": len(expected), "compatible_pairs": sum(pair["compatible"] for pair in pairs),
            "complete_design": complete, "input_records": len(records), "invalid_records": invalid,
            "duplicate_records": sorted(set(duplicates)), "conflicting_run_ids": conflicts,
            "duplicate_slots": duplicate_slots, "missing_pairs": missing, "incompatible_records": incompatible,
            "pairs": pairs, "paired_subset": arms,
            "all_reported_observations": {variant: _arm_summary([row for identity, row in by_id.items()
                                         if row["variant"] == variant and identity not in conflicted_ids]) for variant in VARIANTS},
            "comparison_eligibility": eligibility,
            "deltas_with_memory_minus_no_memory": deltas,
            "record_fingerprints": [{"run_id": identity, "sha256": fingerprint(row)} for identity, row in sorted(by_id.items())],
            "limitations": ["Metrics use all outcomes, including failed tasks, in compatible pairs; never success-only costs.",
                            "Incomplete designs show a descriptive paired subset and disable full-design comparisons.",
                            "Fingerprints and reset flags check runner declarations; they do not attest execution or artifact authenticity.",
                            "Unknown usage remains null; reported cost must include all calls and use one currency for comparison.",
                            "No causal benefit, statistical significance, professional correctness, or net savings is inferred.",
                            "Knowledge-building overhead must be allocated to admin tokens/cost by the runner; no lifecycle cost is inferred."]}
    observed_rows = [row for identity, row in sorted(by_id.items()) if identity not in conflicted_ids]
    if any("artifact_metrics" in task for task in protocol["tasks"]) or any("artifact_metrics" in row for row in observed_rows):
        report["artifact_metrics"] = _artifact_report(protocol, subset, observed_rows, complete)
    return report


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path):
    content = _read_text(path)
    try:
        return json.loads(content, object_pairs_hook=_unique_object,
                          parse_constant=lambda token: (_ for _ in ()).throw(EvaluationError("nonfinite JSON value")))
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"invalid JSON at line {exc.lineno}") from exc


def _read_text(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise EvaluationError("evaluation input file exceeds 20 MiB")
    return path.read_text(encoding="utf-8-sig")


def write_protocol(spec_path, output_path):
    """CLI-friendly JSON spec -> frozen JSON protocol; never overwrite inputs."""
    if Path(spec_path).resolve() == Path(output_path).resolve():
        raise EvaluationError("protocol output must differ from the specification input")
    protocol = create_protocol(_read_json(spec_path))
    with Path(output_path).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(protocol, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return protocol


def report_files(protocol_path, records_path, output_path=None):
    """Read a frozen protocol and external JSONL observations, return traceable report."""
    records = []
    for index, line in enumerate(_read_text(records_path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_unique_object,
                             parse_constant=lambda token: (_ for _ in ()).throw(EvaluationError("nonfinite JSON value")))
        except (json.JSONDecodeError, EvaluationError) as exc:
            raise EvaluationError(f"invalid result JSONL at line {index}: {exc}") from exc
        records.append(row)
        if len(records) > MAX_RECORDS:
            raise EvaluationError("too many evaluation records")
    report = summarize(_read_json(protocol_path), records)
    report["input_file_sha256"] = {"protocol": file_fingerprint(protocol_path), "records": file_fingerprint(records_path)}
    if output_path is not None:
        if Path(output_path).resolve() in {Path(protocol_path).resolve(), Path(records_path).resolve()}:
            raise EvaluationError("report output must differ from protocol and results inputs")
        with Path(output_path).open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return report


def _stop_process_tree(process):
    """Stop the explicit adapter and children on timeout without a shell string."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _adapter_result(argv, request, workspace, environment, timeout_seconds):
    """Bound runtime and imported output; stderr remains private in temporary files."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        started = time.perf_counter()
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output, stderr=errors,
                                       cwd=workspace, env=environment, shell=False, **options)
        except OSError:
            return None, {"status": "launch_failed", "exit_code": None}, (time.perf_counter() - started) * 1000
        try:
            process.communicate(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8"), timeout=timeout_seconds)
            status = "completed" if process.returncode == 0 else "nonzero_exit"
        except subprocess.TimeoutExpired:
            _stop_process_tree(process)
            status = "timeout"
        except BaseException:
            _stop_process_tree(process)
            raise
        elapsed = (time.perf_counter() - started) * 1000
        runner = {"status": status, "exit_code": process.returncode,
                  "stdout_bytes": output.tell(), "stderr_bytes": errors.tell()}
        if status != "completed":
            return None, runner, elapsed
        if output.tell() > 2 * 1024 * 1024:
            runner["status"] = "output_limit_exceeded"
            return None, runner, elapsed
        output.seek(0)
        try:
            result = json.loads(output.read().decode("utf-8-sig"), object_pairs_hook=_unique_object,
                                parse_constant=lambda token: (_ for _ in ()).throw(EvaluationError("nonfinite JSON value")))
            _object(result, "adapter result")
            if set(result) - {"acceptance", "retrieval", "token_usage", "cost", "artifacts", "artifact_metrics", "initial_state_sha256"}:
                raise EvaluationError("adapter result contains reserved fields")
        except (ValueError, UnicodeError, EvaluationError):
            runner["status"] = "invalid_output"
            return None, runner, elapsed
        return result, runner, elapsed


def run_protocol(protocol_path, adapter_path, output_path, timeout_seconds=300):
    """Explicitly run a user-supplied argv adapter in independent temporary roots.

    Adapter JSON: {argv: [executable, ...], knowledge_snapshot_path: FILE,
    data_class: real_adapter|synthetic_template}. A frozen snapshot is verified
    before running. Only with_memory receives its private copy. Adapter stdin
    carries task/conditions/paths; stdout carries observation fields plus the
    prepared dataset's initial_state_sha256. Optional argv_path_indices explicitly
    mark file arguments resolved relative to the adapter JSON, without guessing
    which ordinary arguments are paths. The runner never selects a model.
    All attempts, including timeout/invalid output, are appended immediately.

    This is isolation of evaluation inputs and homes, not an OS security sandbox:
    the adapter is trusted executable code with the invoking user's permissions.
    """
    _number(timeout_seconds, "timeout_seconds")
    if not 0 < timeout_seconds <= 86400:
        raise EvaluationError("timeout_seconds must be greater than zero and at most 86400")
    protocol = _protocol(_read_json(protocol_path))
    adapter = _object(_read_json(adapter_path), "adapter")
    argv = adapter.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 100 or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
        raise EvaluationError("adapter.argv must be a nonempty explicit string array")
    executable = shutil.which(argv[0])
    if not executable:
        raise EvaluationError("adapter executable not found")
    argv = [str(Path(executable).resolve()), *argv[1:]]
    path_indices = adapter.get("argv_path_indices", [])
    if not isinstance(path_indices, list) or any(isinstance(index, bool) or not isinstance(index, int) or not 1 <= index < len(argv) for index in path_indices):
        raise EvaluationError("adapter.argv_path_indices must contain explicit non-executable argument indices")
    if len(set(path_indices)) != len(path_indices):
        raise EvaluationError("duplicate adapter argv path index")
    for index in path_indices:
        path = Path(argv[index])
        if not path.is_absolute():
            path = Path(adapter_path).resolve().parent / path
        if not path.is_file():
            raise EvaluationError("adapter file argument does not exist")
        argv[index] = str(path.resolve())
    if not isinstance(adapter.get("data_class"), str) or adapter["data_class"] not in {"real_adapter", "synthetic_template"}:
        raise EvaluationError("adapter.data_class must explicitly declare real_adapter or synthetic_template")
    if protocol["data_class"] == "real_protocol" and adapter["data_class"] != "real_adapter":
        raise EvaluationError("a synthetic adapter cannot produce a real task evaluation")
    snapshot = Path(_text(adapter.get("knowledge_snapshot_path"), "knowledge_snapshot_path", 4000))
    if not snapshot.is_absolute():
        snapshot = Path(adapter_path).resolve().parent / snapshot
    if not snapshot.is_file() or file_fingerprint(snapshot) != protocol["knowledge_sha256"]:
        raise EvaluationError("knowledge snapshot does not match the frozen protocol fingerprint")
    output_path = Path(output_path)
    if output_path.resolve() in {Path(protocol_path).resolve(), Path(adapter_path).resolve(), snapshot.resolve()}:
        raise EvaluationError("evaluation output must differ from all inputs")
    task_map = {item["id"]: item for item in protocol["tasks"]}
    records = []
    # Exclusive creation occurs before any adapter launch. Existing results remain intact.
    with output_path.open("x", encoding="utf-8") as destination, tempfile.TemporaryDirectory(prefix="alp-evaluation-") as base:
        for position, scheduled in enumerate(protocol["execution_order"], 1):
            row = _new_record(protocol, scheduled["task_id"], scheduled["repeat"], scheduled["variant"])
            root = Path(base) / str(position)
            workspace = root / "workspace"
            alp_home, codex_home, user_home = root / "alp-home", root / "codex-home", root / "user-home"
            local_appdata, appdata = user_home / "local-appdata", user_home / "appdata"
            xdg_cache, xdg_config, xdg_data = user_home / "cache", user_home / "config", user_home / "data"
            for directory in (workspace, alp_home, codex_home, user_home, local_appdata, appdata, xdg_cache, xdg_config, xdg_data):
                directory.mkdir(parents=True)
            knowledge = None
            if scheduled["variant"] == "with_memory":
                knowledge = root / "frozen-knowledge.snapshot"
                shutil.copyfile(snapshot, knowledge)
                if file_fingerprint(knowledge) != protocol["knowledge_sha256"]:
                    raise EvaluationError("knowledge snapshot changed while preparing a run")
            environment = dict(os.environ)
            for name in list(environment):
                if name.upper().startswith("ALP_") or name.upper() in {"CODEX_HOME", "HOME", "USERPROFILE"}:
                    environment.pop(name, None)
            environment.update(ALP_HOME=str(alp_home), CODEX_HOME=str(codex_home), HOME=str(user_home),
                               USERPROFILE=str(user_home), PYTHONDONTWRITEBYTECODE="1",
                               LOCALAPPDATA=str(local_appdata), APPDATA=str(appdata), XDG_CACHE_HOME=str(xdg_cache),
                               XDG_CONFIG_HOME=str(xdg_config), XDG_DATA_HOME=str(xdg_data),
                               ALP_EVALUATION_VARIANT=scheduled["variant"])
            request = {"protocol_sha256": protocol["protocol_sha256"], "run_id": row["run_id"],
                       "task": task_map[scheduled["task_id"]], "repeat": scheduled["repeat"],
                       "variant": scheduled["variant"], "conditions": row["conditions"],
                       "workspace": str(workspace), "alp_home": str(alp_home), "codex_home": str(codex_home),
                       "knowledge_snapshot_path": str(knowledge) if knowledge else None,
                       "instruction": "Prepare the declared dataset in this fresh workspace. Disable memory in no_memory. "
                                      "Use only the provided frozen snapshot in with_memory. Run the same prompt and acceptance checks. "
                                      "Report actual worker/admin/retrieval/other token observations, including failed attempts; unknown is null."}
            observations, runner, wall_ms = _adapter_result(argv, request, workspace, environment, timeout_seconds)
            row.update(source="real_external" if protocol["data_class"] == "real_protocol" else "synthetic_template",
                       wall_ms=wall_ms, runner=runner)
            row["runner"]["schedule_position"] = position
            row["isolation"] = {"workspace_id": row["run_id"], "reset_verified": False,
                                "initial_state_sha256": task_map[scheduled["task_id"]]["dataset_sha256"]}
            # Fresh roots are runner-managed; dataset preparation must be reported by the adapter.
            if observations is not None:
                if observations.get("initial_state_sha256") != task_map[scheduled["task_id"]]["dataset_sha256"]:
                    row["runner"]["status"] = "dataset_unverified"
                else:
                    observations = {key: value for key, value in observations.items() if key != "initial_state_sha256"}
                    merged = {**row, **observations}
                    merged["isolation"]["reset_verified"] = True
                    try:
                        _record(merged)
                        row = merged
                    except EvaluationError:
                        row["runner"]["status"] = "invalid_observations"
                        row["isolation"]["reset_verified"] = False
            records.append(row)
            destination.write(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
            destination.flush()
    result = summarize(protocol, records)
    result["runner"] = {"adapter_sha256": file_fingerprint(adapter_path), "knowledge_sha256": protocol["knowledge_sha256"],
                        "outcomes": dict(Counter(row["runner"]["status"] for row in records)), "timeout_seconds": timeout_seconds,
                        "homes": "independent_temporary_workspace_ALP_HOME_CODEX_HOME_HOME",
                        "limitation": "Trusted adapter code; input isolation is not an OS sandbox. Dataset initialization, model use and acceptance remain adapter observations."}
    result["input_file_sha256"] = {"protocol": file_fingerprint(protocol_path), "records": file_fingerprint(output_path),
                                  "adapter": file_fingerprint(adapter_path)}
    return result
