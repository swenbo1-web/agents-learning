"""Offline paired-evaluation protections; all generated results are synthetic."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agent_learning.task_evaluation import (EvaluationError, TOKEN_COMPONENTS, create_protocol,
                                           file_fingerprint, fingerprint, record_template,
                                           report_files, run_protocol, summarize, write_protocol)


def spec():
    return {"id": "paired-offline-fixture", "data_class": "real_protocol", "repeats": 2, "seed": 42,
            "model": {"provider": "fixture", "name": "fixture-model", "version": "1", "parameters": {"seed": 1}},
            "environment_sha256": fingerprint({"platform": "fixture"}), "knowledge_sha256": fingerprint("knowledge"),
            "tasks": [{"id": "fix-test", "prompt": "Fix the failing test in the frozen fixture.",
                       "dataset_sha256": fingerprint("fixture"), "acceptance_criteria": "All specified offline fixture checks pass."}]}


def measured_rows(protocol):
    rows = []
    for position, slot in enumerate(protocol["execution_order"]):
        row = record_template(protocol, **slot)
        row["run_id"] = f"run-{position}"
        # Real_external is a validator fixture here, not an actual model execution.
        row["source"] = "real_external"
        row["acceptance"] = {"status": "passed", "method": "automated", "evidence_sha256": fingerprint("checked")}
        row["wall_ms"] = 200 if row["variant"] == "no_memory" else 100
        row["retrieval"] = {"latencies_ms": [] if row["variant"] == "no_memory" else [2, 4],
                            "hit": None if row["variant"] == "no_memory" else True}
        count = 100 if row["variant"] == "no_memory" else 50
        row["token_usage"] = {component: {"input_tokens": count if component == "worker" else 0,
                                         "output_tokens": 0, "total_tokens": count if component == "worker" else 0}
                              for component in TOKEN_COMPONENTS}
        row["cost"] = {"amount": .1 if row["variant"] == "no_memory" else .05, "currency": "USD", "scope": "all_calls"}
        row["isolation"] = {"workspace_id": f"workspace-{position}", "reset_verified": True,
                            "initial_state_sha256": protocol["tasks"][0]["dataset_sha256"]}
        rows.append(row)
    return rows


def artifact_spec():
    definition = spec()
    definition["tasks"][0]["artifact_metrics"] = [
        {"id": "latency", "kind": "performance", "unit": "ms", "method": "Median of 10 frozen workload requests.",
         "criteria": {"operator": "lte", "value": 10}},
        {"id": "peak", "kind": "memory", "unit": "bytes", "method": "Peak resident bytes during the frozen workload.",
         "criteria": {"operator": "lte", "value": 1024}},
        {"id": "accuracy", "kind": "quality", "unit": "ratio", "method": "Correct outputs divided by frozen test cases.",
         "criteria": {"operator": "gte", "value": .9}}]
    return definition


def artifact_rows(protocol):
    rows = measured_rows(protocol)
    for row in rows:
        for item in row.get("artifact_metrics", []):
            item.update(value={"latency": 5, "peak": 1024, "accuracy": .95}[item["id"]],
                        artifact_sha256=fingerprint("generated artifact " + row["run_id"]),
                        evidence_sha256=fingerprint("measurement " + row["run_id"]))
    return rows


class ProtocolTests(unittest.TestCase):
    def test_legacy_protocol_and_report_fingerprints_remain_identical(self):
        protocol = create_protocol(spec())
        self.assertEqual(protocol["protocol_sha256"], "3500dce88f73bc243c8b314885bafb4cf9b86e8f053ac851a5f23cdff4e9d63b")
        report = summarize(protocol, measured_rows(protocol))
        self.assertEqual(fingerprint(report), "f537e46361dad5a017e0f6b1bc399bdf54969972e610ca3dbc84a163f5ce8dc5")
        self.assertNotIn("artifact_metrics", protocol["tasks"][0])
        self.assertNotIn("artifact_metrics", report)
    def test_protocol_is_stable_and_order_balanced(self):
        first = create_protocol(spec())
        self.assertEqual(first, create_protocol(spec()))
        firsts = [first["execution_order"][index]["variant"] for index in (0, 2)]
        self.assertEqual(set(firsts), {"no_memory", "with_memory"})

    def test_require_multiple_repeats_and_explicit_source(self):
        for key, value in (("repeats", 1), ("data_class", None), ("data_class", []),
                           ("data_class", {}), ("seed", True)):
            definition = spec()
            definition[key] = value
            with self.assertRaises(EvaluationError):
                create_protocol(definition)

    def test_protocol_version_requires_the_declared_integer_type(self):
        for version in (True, 1.0, [], {}):
            protocol = create_protocol(spec())
            protocol["version"] = version
            with self.subTest(version=version), self.assertRaises(EvaluationError):
                summarize(protocol, [])

    def test_changed_frozen_prompt_is_rejected(self):
        protocol = create_protocol(spec())
        protocol["tasks"][0]["prompt"] = "different prompt"
        with self.assertRaises(EvaluationError):
            summarize(protocol, [])

    def test_template_is_not_measured(self):
        protocol = create_protocol(spec())
        template = record_template(protocol, "fix-test", 1, "no_memory")
        report = summarize(protocol, [template])
        self.assertFalse(report["complete_design"])
        self.assertIsNone(report["deltas_with_memory_minus_no_memory"]["total_tokens"])
        self.assertIn("synthetic_or_template_input", report["incompatible_records"][0]["reasons"])

    def test_synthetic_protocol_never_produces_real_comparison(self):
        definition = spec()
        definition["data_class"] = "synthetic_template"
        protocol = create_protocol(definition)
        report = summarize(protocol, measured_rows(protocol))
        self.assertFalse(report["complete_design"])
        self.assertFalse(report["comparison_eligibility"]["total_tokens"]["eligible"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.protocol = create_protocol(spec())
        self.rows = measured_rows(self.protocol)

    def test_complete_cost_includes_failed_attempts_and_admin(self):
        for row in self.rows:
            if row["variant"] == "with_memory" and row["repeat"] == 2:
                row["acceptance"]["status"] = "failed"
                row["token_usage"]["worker"] = {"input_tokens": 500, "output_tokens": 0, "total_tokens": 500}
                row["token_usage"]["admin"] = {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
                row["cost"]["amount"] = 1
        report = summarize(self.protocol, self.rows)
        self.assertTrue(report["complete_design"])
        self.assertEqual(report["paired_subset"]["with_memory"]["acceptance"]["success_rate"], .5)
        self.assertEqual(report["paired_subset"]["with_memory"]["token_usage"]["total_tokens"], 600)
        self.assertEqual(report["deltas_with_memory_minus_no_memory"]["total_tokens"], 400)
        self.assertAlmostEqual(report["deltas_with_memory_minus_no_memory"]["monetary_cost"]["amount"], .85)

    def test_missing_pair_disables_entire_design_but_reports_observed_costs(self):
        report = summarize(self.protocol, self.rows[:-1])
        self.assertFalse(report["complete_design"])
        self.assertEqual(report["compatible_pairs"], 1)
        self.assertEqual(len(report["missing_pairs"]), 1)
        self.assertTrue(all(not item["eligible"] for item in report["comparison_eligibility"].values()))
        self.assertEqual(sum(arm["runs"] for arm in report["all_reported_observations"].values()), 3)

    def test_unknown_tokens_stay_null_not_zero(self):
        self.rows[0]["token_usage"]["admin"] = None
        report = summarize(self.protocol, self.rows)
        self.assertTrue(report["complete_design"])
        self.assertFalse(report["comparison_eligibility"]["total_tokens"]["eligible"])
        self.assertIsNone(report["deltas_with_memory_minus_no_memory"]["total_tokens"])
        self.assertTrue(report["comparison_eligibility"]["wall_latency"]["eligible"])

    def test_partial_token_fields_remain_reported_without_full_savings(self):
        self.rows[0]["token_usage"]["worker"] = {"input_tokens": 77, "output_tokens": None, "total_tokens": None}
        report = summarize(self.protocol, self.rows)
        self.assertFalse(report["invalid_records"])
        self.assertFalse(report["comparison_eligibility"]["total_tokens"]["eligible"])
        component = report["paired_subset"][self.rows[0]["variant"]]["token_usage"]["components"]["worker"]
        self.assertGreaterEqual(component["reported_input_tokens"], 77)
        self.assertEqual(component["unknown_runs"], 1)
        self.assertIsNone(component["complete_total_tokens"])

    def test_currency_mismatch_prevents_money_comparison(self):
        self.rows[0]["cost"]["currency"] = "EUR"
        report = summarize(self.protocol, self.rows)
        self.assertFalse(report["comparison_eligibility"]["monetary_cost"]["eligible"])
        self.assertIsNone(report["deltas_with_memory_minus_no_memory"]["monetary_cost"])

    def test_unknown_acceptance_prevents_success_claim(self):
        self.rows[0]["acceptance"] = {"method": "automated", "status": "unknown", "evidence_sha256": None}
        report = summarize(self.protocol, self.rows)
        self.assertFalse(report["comparison_eligibility"]["success_rate"]["eligible"])
        self.assertIsNone(report["paired_subset"][self.rows[0]["variant"]]["acceptance"]["success_rate"])

    def test_known_acceptance_needs_evidence(self):
        self.rows[0]["acceptance"]["evidence_sha256"] = None
        report = summarize(self.protocol, self.rows)
        self.assertTrue(report["invalid_records"])
        self.assertFalse(report["complete_design"])

    def test_equivalent_duplicate_is_idempotent_but_tampering_conflicts(self):
        report = summarize(self.protocol, self.rows + [copy.deepcopy(self.rows[0])])
        self.assertTrue(report["complete_design"])
        self.assertEqual(report["duplicate_records"], [self.rows[0]["run_id"]])
        tampered = copy.deepcopy(self.rows[0])
        tampered["wall_ms"] += 1
        report = summarize(self.protocol, self.rows + [tampered])
        self.assertFalse(report["complete_design"])
        self.assertEqual(report["conflicting_run_ids"], [self.rows[0]["run_id"]])

    def test_duplicate_slot_is_not_extra_sample(self):
        duplicate = copy.deepcopy(self.rows[0])
        duplicate["run_id"] = "another-attempt"
        duplicate["isolation"]["workspace_id"] = "another-workspace"
        report = summarize(self.protocol, self.rows + [duplicate])
        self.assertFalse(report["complete_design"])
        self.assertEqual(len(report["duplicate_slots"]), 1)

    def test_condition_change_and_workspace_reuse_are_incompatible(self):
        self.rows[0]["conditions"]["model"]["name"] = "different"
        # Templates may share protocol nested model objects; create a fresh frozen protocol.
        protocol = create_protocol(spec())
        self.rows[0]["condition_sha256"] = fingerprint(self.rows[0]["conditions"])
        self.rows[1]["isolation"]["workspace_id"] = self.rows[0]["isolation"]["workspace_id"]
        report = summarize(protocol, self.rows)
        self.assertFalse(report["complete_design"])
        reasons = [reason for item in report["incompatible_records"] for reason in item["reasons"]]
        self.assertIn("model_environment_prompt_dataset_acceptance_or_memory_mismatch", reasons)
        self.assertIn("workspace_reused_across_runs", reasons)

    def test_bool_negative_nan_and_inconsistent_tokens_invalid(self):
        for value in (True, -1, float("nan")):
            rows = copy.deepcopy(self.rows)
            rows[0]["wall_ms"] = value
            self.assertTrue(summarize(self.protocol, rows)["invalid_records"])
        self.rows[0]["token_usage"]["worker"]["total_tokens"] += 1
        self.assertTrue(summarize(self.protocol, self.rows)["invalid_records"])

    def test_report_is_stable_regardless_import_order(self):
        self.assertEqual(summarize(self.protocol, self.rows), summarize(self.protocol, list(reversed(self.rows))))

    def test_p50_and_p95_are_defined(self):
        for position, row in enumerate(self.rows):
            if row["variant"] == "with_memory":
                row["retrieval"]["latencies_ms"] = [1, 2, 3, 100]
        report = summarize(self.protocol, self.rows)
        distribution = report["paired_subset"]["with_memory"]["retrieval_latency_ms"]
        self.assertEqual(distribution["p50"], 2.5)
        self.assertEqual(distribution["p95"], 100)


class ArtifactMetricTests(unittest.TestCase):
    def setUp(self):
        self.protocol = create_protocol(artifact_spec())
        self.rows = artifact_rows(self.protocol)

    def group(self, report, variant="with_memory", identity="latency"):
        return next(item for item in report["artifact_metrics"]["groups"][variant] if item["metric_id"] == identity)

    def test_conditions_freeze_method_units_criteria_and_fingerprints(self):
        declaration = self.protocol["tasks"][0]["artifact_metrics"][0]
        self.assertEqual(declaration["criteria_sha256"], fingerprint(declaration["criteria"]))
        self.assertEqual(self.rows[0]["conditions"]["artifact_metrics_sha256"],
                         fingerprint(self.protocol["tasks"][0]["artifact_metrics"]))
        changed = copy.deepcopy(self.protocol)
        changed["tasks"][0]["artifact_metrics"][0]["criteria"]["value"] = 20
        with self.assertRaises(EvaluationError):
            summarize(changed, [])
        definition = artifact_spec()
        definition["tasks"][0]["artifact_metrics"][0]["method"] = "A different workload method."
        self.assertNotEqual(create_protocol(definition)["protocol_sha256"], self.protocol["protocol_sha256"])

    def test_product_metrics_are_separate_from_task_time_tokens_and_acceptance(self):
        for row in self.rows:
            if row["variant"] == "with_memory":
                for item in row["artifact_metrics"]:
                    item["value"] = {"latency": 20, "peak": 2048, "accuracy": .8}[item["id"]]
        report = summarize(self.protocol, self.rows)
        self.assertTrue(report["comparison_eligibility"]["total_tokens"]["eligible"])
        self.assertLess(report["deltas_with_memory_minus_no_memory"]["wall_ms_total"], 0)
        self.assertEqual(self.group(report)["values"]["p50"], 20)
        self.assertEqual(self.group(report, "no_memory")["values"]["p50"], 5)
        for identity in ("latency", "peak", "accuracy"):
            self.assertEqual(self.group(report, identity=identity)["criteria_results"]["status"], "failed")
        self.assertEqual(report["paired_subset"]["with_memory"]["acceptance"]["success_rate"], 1)
        self.assertFalse(report["artifact_metrics"]["comparisons_inferred"])
        self.assertTrue(self.group(report)["evidence"][0]["artifact_sha256"])

    def test_missing_observations_are_unknown_and_zero_is_measured(self):
        for row in self.rows:
            if row["variant"] == "with_memory":
                row.pop("artifact_metrics")
            else:
                row["artifact_metrics"][0]["value"] = 0
        report = summarize(self.protocol, self.rows)
        unknown = self.group(report)
        self.assertEqual(unknown["unknown_runs"], 2)
        self.assertIsNone(unknown["values"]["p50"])
        self.assertEqual(unknown["criteria_results"]["status"], "unknown")
        self.assertEqual(self.group(report, "no_memory")["values"]["p50"], 0)
        self.assertTrue(report["complete_design"])

    def test_unknown_template_and_missing_criteria_never_pass(self):
        unknown = record_template(self.protocol, "fix-test", 1, "no_memory")["artifact_metrics"][0]
        self.assertIsNone(unknown["value"])
        self.assertIsNone(unknown["evidence_sha256"])
        definition = artifact_spec()
        definition["tasks"][0]["artifact_metrics"][0].pop("criteria")
        protocol = create_protocol(definition)
        report = summarize(protocol, artifact_rows(protocol))
        group = self.group(report)
        self.assertEqual(group["known_runs"], 2)
        self.assertIsNone(group["criteria_sha256"])
        self.assertEqual(group["criteria_results"], {"passed": 0, "failed": 0, "unknown": 2, "status": "unknown"})

    def test_unit_method_and_criteria_mismatches_are_not_pooled(self):
        for field, value in (("unit", "s"), ("method", "Different benchmark."), ("criteria_sha256", "a" * 64)):
            with self.subTest(field=field):
                rows = copy.deepcopy(self.rows)
                selected = next(row for row in rows if row["variant"] == "with_memory")
                selected["artifact_metrics"][0][field] = value
                report = summarize(self.protocol, rows)
                self.assertTrue(report["complete_design"])
                self.assertEqual(self.group(report)["known_runs"], 1)
                self.assertEqual(self.group(report)["mismatched_runs"], 1)
                self.assertEqual(self.group(report)["criteria_results"]["status"], "unknown")
                self.assertEqual(report["artifact_metrics"]["unmatched_observations"][0]["reason"], "unit_method_or_criteria_mismatch")

    def test_invalid_measurements_require_units_method_and_evidence_sha(self):
        bad = [("unit", "MB"), ("method", ""), ("value", True), ("value", -1),
               ("value", float("nan")), ("value", float("inf")), ("value", 10 ** 1000),
               ("evidence_sha256", None), ("artifact_sha256", None), ("evidence_sha256", "BAD")]
        for field, value in bad:
            with self.subTest(field=field, value=str(value)[:30]):
                rows = copy.deepcopy(self.rows)
                rows[0]["artifact_metrics"][0][field] = value
                self.assertTrue(summarize(self.protocol, rows)["invalid_records"])
        for index, value in ((1, 1.5), (2, 1.01)):
            rows = copy.deepcopy(self.rows)
            rows[0]["artifact_metrics"][index]["value"] = value
            self.assertTrue(summarize(self.protocol, rows)["invalid_records"])
        rows = copy.deepcopy(self.rows)
        rows[0]["artifact_metrics"].append(copy.deepcopy(rows[0]["artifact_metrics"][0]))
        self.assertTrue(summarize(self.protocol, rows)["invalid_records"])

    def test_frozen_declarations_reject_ambiguous_criteria_and_duplicate_ids(self):
        for criteria in ({"operator": "approximately", "value": 10}, {"operator": [], "value": 10},
                         {"operator": "lte", "value": True}, {"operator": "lte"}, "somehow faster"):
            definition = artifact_spec()
            definition["tasks"][0]["artifact_metrics"][0]["criteria"] = criteria
            with self.subTest(criteria=criteria), self.assertRaises(EvaluationError):
                create_protocol(definition)
        definition = artifact_spec()
        definition["tasks"][0]["artifact_metrics"].append(copy.deepcopy(definition["tasks"][0]["artifact_metrics"][0]))
        with self.assertRaises(EvaluationError):
            create_protocol(definition)

    def test_incomplete_design_and_undeclared_metrics_do_not_claim_pass(self):
        report = summarize(self.protocol, self.rows[:-1])
        self.assertEqual(self.group(report)["criteria_results"]["status"], "unknown")
        protocol = create_protocol(spec())
        rows = measured_rows(protocol)
        for row, source in zip(rows, self.rows):
            row["artifact_metrics"] = source["artifact_metrics"]
        report = summarize(protocol, rows)
        self.assertTrue(report["complete_design"])
        self.assertEqual(report["artifact_metrics"]["groups"], {"no_memory": [], "with_memory": []})
        self.assertTrue(all(item["reason"] == "metric_not_predeclared"
                            for item in report["artifact_metrics"]["unmatched_observations"]))

    def test_tasks_are_separate_and_failed_task_observations_remain_visible(self):
        definition = artifact_spec()
        second = copy.deepcopy(definition["tasks"][0])
        second["id"] = "second-artifact"
        second["artifact_metrics"][0]["method"] = "A separately frozen workload."
        definition["tasks"].append(second)
        protocol = create_protocol(definition)
        rows = artifact_rows(protocol)
        for row in rows:
            if row["task_id"] == "second-artifact":
                row["artifact_metrics"][0]["value"] = 30
            row["acceptance"]["status"] = "failed"
        report = summarize(protocol, rows)
        groups = [item for item in report["artifact_metrics"]["groups"]["with_memory"] if item["metric_id"] == "latency"]
        self.assertEqual({item["values"]["p50"] for item in groups}, {5, 30})
        self.assertTrue(all(item["known_runs"] == 2 for item in groups))
        self.assertEqual(report, summarize(protocol, list(reversed(rows))))

    def test_signed_scores_and_large_finite_medians_remain_finite(self):
        definition = artifact_spec()
        quality = definition["tasks"][0]["artifact_metrics"][2]
        quality.update(unit="score", criteria={"operator": "gte", "value": -5})
        protocol = create_protocol(definition)
        rows = artifact_rows(protocol)
        for row in rows:
            row["artifact_metrics"][2]["value"] = 1e308 if row["variant"] == "with_memory" else -2
        report = summarize(protocol, rows)
        self.assertEqual(self.group(report, identity="accuracy")["values"]["p50"], 1e308)
        self.assertEqual(self.group(report, "no_memory", "accuracy")["values"]["p50"], -2)
        json.dumps(report, allow_nan=False)


class NumericBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.protocol = create_protocol(spec())
        self.rows = measured_rows(self.protocol)

    def test_legal_json_large_integer_is_invalid_record_instead_of_a_crash(self):
        huge = json.loads("1" + "0" * 400)
        for field, value in (("wall_ms", huge), ("latency", huge), ("cost", huge),
                             ("wall_ms", True), ("latency", False), ("cost", True)):
            with self.subTest(field=field, value=str(value)[:32]):
                rows = copy.deepcopy(self.rows)
                if field == "wall_ms":
                    rows[0][field] = value
                elif field == "latency":
                    rows[0]["retrieval"]["latencies_ms"] = [value]
                else:
                    rows[0]["cost"]["amount"] = value
                report = summarize(self.protocol, rows)
                self.assertEqual(len(report["invalid_records"]), 1)
                self.assertIn("finite nonnegative", report["invalid_records"][0]["reason"])
                self.assertFalse(report["complete_design"])
                self.assertIsNone(report["deltas_with_memory_minus_no_memory"]["wall_ms_total"])
                json.dumps(report, allow_nan=False)

    def test_finite_large_totals_stay_unknown_without_erasing_other_metrics(self):
        for row in self.rows:
            row["wall_ms"] = 1e308
            row["retrieval"]["latencies_ms"] = [1e308, 1e308]
            row["cost"]["amount"] = 1e308
        report = summarize(self.protocol, self.rows)
        self.assertTrue(report["complete_design"])
        for variant in ("no_memory", "with_memory"):
            for scope in ("paired_subset", "all_reported_observations"):
                arm = report[scope][variant]
                self.assertEqual(arm["wall_ms"]["p50"], 1e308)
                self.assertEqual(arm["wall_ms"]["p95"], 1e308)
                self.assertIsNone(arm["wall_ms"]["sum"])
                self.assertEqual(arm["wall_ms"]["numeric_overflow"], ["sum"])
                self.assertIsNone(arm["retrieval_latency_ms"]["sum"])
                self.assertIsNone(arm["cost"]["reported_by_currency"]["USD"])
                self.assertEqual(arm["cost"]["numeric_overflow"], ["USD"])
                self.assertEqual(arm["cost"]["known_runs"], 2)
                self.assertEqual(arm["cost"]["unknown_runs"], 0)
                self.assertEqual(arm["acceptance"]["success_rate"], 1)
        for metric, reason in (("wall_latency", "wall_latency_numeric_overflow"),
                               ("retrieval_latency", "retrieval_latency_numeric_overflow"),
                               ("monetary_cost", "cost_numeric_overflow")):
            self.assertFalse(report["comparison_eligibility"][metric]["eligible"])
            self.assertEqual(report["comparison_eligibility"][metric]["reason"], reason)
        self.assertIsNone(report["deltas_with_memory_minus_no_memory"]["wall_ms_total"])
        self.assertIsNone(report["deltas_with_memory_minus_no_memory"]["monetary_cost"])
        self.assertTrue(report["comparison_eligibility"]["total_tokens"]["eligible"])
        json.dumps(report, allow_nan=False)

    def test_one_arm_overflow_does_not_turn_cross_arm_difference_into_infinity(self):
        for row in self.rows:
            if row["variant"] == "with_memory":
                row["wall_ms"] = 1e308
                row["cost"]["amount"] = 1e308
        report = summarize(self.protocol, self.rows)
        self.assertEqual(report["paired_subset"]["no_memory"]["wall_ms"]["sum"], 400)
        self.assertIsNone(report["paired_subset"]["with_memory"]["wall_ms"]["sum"])
        self.assertFalse(report["comparison_eligibility"]["wall_latency"]["eligible"])
        self.assertFalse(report["comparison_eligibility"]["monetary_cost"]["eligible"])
        self.assertIsNone(report["deltas_with_memory_minus_no_memory"]["wall_ms_total"])
        json.dumps(report, allow_nan=False)

    def test_large_finite_values_that_fit_remain_comparable(self):
        for row in self.rows:
            row["wall_ms"] = 8e307 if row["variant"] == "no_memory" else 1e307
            row["retrieval"]["latencies_ms"] = [row["wall_ms"]]
            row["cost"]["amount"] = row["wall_ms"]
        report = summarize(self.protocol, self.rows)
        self.assertTrue(report["comparison_eligibility"]["wall_latency"]["eligible"])
        self.assertTrue(report["comparison_eligibility"]["monetary_cost"]["eligible"])
        self.assertEqual(report["paired_subset"]["no_memory"]["wall_ms"]["sum"], 1.6e308)
        self.assertEqual(report["deltas_with_memory_minus_no_memory"]["wall_ms_total"], -1.4e308)
        self.assertEqual(report["deltas_with_memory_minus_no_memory"]["monetary_cost"]["amount"], -1.4e308)
        json.dumps(report, allow_nan=False)

    def test_nonfinite_float_and_negative_observations_are_isolated(self):
        for value in (float("inf"), float("-inf"), float("nan"), -1):
            for field in ("wall_ms", "latency", "cost"):
                with self.subTest(field=field, value=value):
                    rows = copy.deepcopy(self.rows)
                    if field == "wall_ms":
                        rows[0][field] = value
                    elif field == "latency":
                        rows[0]["retrieval"]["latencies_ms"] = [value]
                    else:
                        rows[0]["cost"]["amount"] = value
                    report = summarize(self.protocol, rows)
                    self.assertEqual(len(report["invalid_records"]), 1)
                    json.dumps(report, allow_nan=False)

    def test_signed_artifact_scores_and_huge_criteria_share_finite_number_contract(self):
        definition = artifact_spec()
        quality = definition["tasks"][0]["artifact_metrics"][2]
        quality.update(unit="score", criteria={"operator": "gte", "value": -1e308})
        protocol = create_protocol(definition)
        rows = artifact_rows(protocol)
        for position, row in enumerate(rows):
            row["artifact_metrics"][2]["value"] = 1e308 if position % 2 else -1e308
        report = summarize(protocol, rows)
        for variant in ("no_memory", "with_memory"):
            group = next(item for item in report["artifact_metrics"]["groups"][variant]
                         if item["metric_id"] == "accuracy")
            self.assertIn(group["values"]["p50"], (-1e308, 0, 1e308))
            self.assertEqual(group["criteria_results"]["status"], "passed")
        json.dumps(report, allow_nan=False)
        quality["criteria"]["value"] = 10 ** 400
        with self.assertRaisesRegex(EvaluationError, "finite number"):
            create_protocol(definition)
        rows[0]["artifact_metrics"][2]["value"] = 10 ** 400
        self.assertEqual(len(summarize(protocol, rows)["invalid_records"]), 1)


class FileAndRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, content):
        path = self.root / name
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def test_extreme_numeric_jsonl_roundtrip_writes_a_strict_finite_report(self):
        frozen = self.root / "finite-protocol.json"
        protocol = write_protocol(self.write("finite-spec.json", spec()), frozen)
        for label in ("huge-integer", "finite-total-overflow"):
            rows = measured_rows(protocol)
            if label == "huge-integer":
                rows[0]["wall_ms"] = 10 ** 400
            else:
                for row in rows:
                    row["wall_ms"] = 1e308
                    row["cost"]["amount"] = 1e308
            records = self.root / (label + ".jsonl")
            records.write_text("\n".join(json.dumps(row, allow_nan=False) for row in rows), encoding="utf-8")
            destination = self.root / (label + "-report.json")
            report = report_files(frozen, records, destination)
            loaded = json.loads(destination.read_text(encoding="utf-8"),
                parse_constant=lambda value: self.fail("nonfinite output: " + value))
            self.assertEqual(report, loaded)
            self.assertIsNone(report["deltas_with_memory_minus_no_memory"]["wall_ms_total"])
            self.assertEqual(bool(report["invalid_records"]), label == "huge-integer")

    def test_jsonl_roundtrip_trace_and_no_overwrite(self):
        definition = self.write("spec.json", spec())
        frozen = self.root / "protocol.json"
        protocol = write_protocol(definition, frozen)
        rows = measured_rows(protocol)
        records = self.root / "records.jsonl"
        records.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        destination = self.root / "report.json"
        report = report_files(frozen, records, destination)
        self.assertTrue(report["complete_design"])
        self.assertEqual(report["input_file_sha256"]["records"], file_fingerprint(records))
        with self.assertRaises(FileExistsError):
            report_files(frozen, records, destination)
        with self.assertRaises(EvaluationError):
            report_files(frozen, records, records)
        with self.assertRaises(FileExistsError):
            write_protocol(definition, frozen)

    def test_malformed_jsonl_enums_are_invalid_records_without_erasing_other_runs(self):
        frozen = self.root / "protocol.json"
        protocol = write_protocol(self.write("spec.json", spec()), frozen)
        malformed = ((("source",), []), (("source",), {}), (("variant",), []),
                     (("conditions", "memory_mode"), {}), (("acceptance", "method"), []),
                     (("acceptance", "status"), {}), (("cost", "scope"), []))
        for index, (fields, value) in enumerate(malformed):
            with self.subTest(fields=fields, value=value):
                rows = measured_rows(protocol)
                target = rows[0]
                for field in fields[:-1]:
                    target = target[field]
                target[fields[-1]] = value
                records = self.root / f"malformed-{index}.jsonl"
                records.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
                destination = self.root / f"malformed-{index}-report.json"
                report = report_files(frozen, records, destination)
                self.assertEqual(report, json.loads(destination.read_text(encoding="utf-8")))
                self.assertEqual(len(report["invalid_records"]), 1)
                self.assertEqual(report["invalid_records"][0]["record"], 1)
                self.assertEqual(sum(arm["runs"] for arm in report["all_reported_observations"].values()), 3)
                self.assertFalse(report["complete_design"])
                self.assertTrue(all(not item["eligible"] for item in report["comparison_eligibility"].values()))

    def test_duplicate_json_keys_rejected(self):
        frozen = self.write("protocol.json", create_protocol(spec()))
        records = self.root / "records.jsonl"
        records.write_text('{"run_id":"a","run_id":"b"}\n', encoding="utf-8")
        with self.assertRaises(EvaluationError):
            report_files(frozen, records)

    def runner_fixture(self, script_body, real=False):
        snapshot = self.root / "knowledge.json"
        snapshot.write_text('{"data_class":"synthetic_template","entries":[]}', encoding="utf-8")
        definition = spec()
        definition["data_class"] = "real_protocol" if real else "synthetic_template"
        definition["knowledge_sha256"] = file_fingerprint(snapshot)
        frozen = self.write("protocol.json", create_protocol(definition))
        script = self.root / "adapter.py"
        script.write_text(script_body, encoding="utf-8")
        adapter = self.write("adapter.json", {"argv": [sys.executable, "-B", str(script)],
                                              "knowledge_snapshot_path": str(snapshot),
                                              "data_class": "real_adapter" if real else "synthetic_template"})
        return frozen, adapter, self.root / "results.jsonl"

    def test_explicit_fake_adapter_has_isolated_homes_and_frozen_snapshot(self):
        frozen, adapter, output = self.runner_fixture('''import json, os, pathlib, sys
request=json.load(sys.stdin)
assert pathlib.Path.cwd()==pathlib.Path(request["workspace"])
assert os.environ["ALP_HOME"]==request["alp_home"]
assert os.environ["CODEX_HOME"]==request["codex_home"]
assert os.environ["LOCALAPPDATA"].startswith(os.environ["HOME"])
assert os.environ["XDG_CONFIG_HOME"].startswith(os.environ["HOME"])
assert not list(pathlib.Path(request["workspace"]).iterdir())
assert bool(request["knowledge_snapshot_path"])==(request["variant"]=="with_memory")
print(json.dumps({"acceptance":{"method":"automated","status":"passed","evidence_sha256":"a"*64},
                  "initial_state_sha256":request["task"]["dataset_sha256"]}))
''')
        report = run_protocol(frozen, adapter, output, timeout_seconds=10)
        self.assertEqual(report["runner"]["outcomes"], {"completed": 4})
        self.assertFalse(report["complete_design"])
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(len({row["isolation"]["workspace_id"] for row in rows}), 4)
        self.assertTrue(all(row["source"] == "synthetic_template" for row in rows))
        with self.assertRaises(FileExistsError):
            run_protocol(frozen, adapter, output)

    def test_explicit_fake_adapter_reports_separate_artifact_observations(self):
        frozen, adapter, output = self.runner_fixture('''import json,sys
request=json.load(sys.stdin)
item=request["task"]["artifact_metrics"][0]
measurement={key:item[key] for key in ("id","kind","unit","method","criteria_sha256")}
measurement.update(value=24,artifact_sha256="b"*64,evidence_sha256="c"*64)
print(json.dumps({"initial_state_sha256":request["task"]["dataset_sha256"],"artifact_metrics":[measurement]}))
''')
        definition = artifact_spec()
        previous = json.loads(frozen.read_text())
        definition.update(data_class="synthetic_template", knowledge_sha256=previous["knowledge_sha256"])
        frozen.write_text(json.dumps(create_protocol(definition)), encoding="utf-8")
        report = run_protocol(frozen, adapter, output, timeout_seconds=10)
        self.assertEqual(report["runner"]["outcomes"], {"completed": 4})
        records = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertTrue(all(row["artifact_metrics"][0]["value"] == 24 for row in records))
        self.assertTrue(all(row["wall_ms"] > 0 for row in records))
        self.assertTrue(all(row["acceptance"]["status"] == "unknown" for row in records))
        self.assertFalse(report["complete_design"])
        for groups in report["artifact_metrics"]["groups"].values():
            self.assertTrue(all(group["criteria_results"]["status"] == "unknown" for group in groups))

    def test_explicit_relative_script_argument_resolves_against_adapter_json(self):
        frozen, adapter, output = self.runner_fixture('''import json,sys
request=json.load(sys.stdin)
print(json.dumps({"initial_state_sha256":request["task"]["dataset_sha256"]}))
''')
        definition = json.loads(adapter.read_text())
        definition["argv"][-1] = "adapter.py"
        definition["argv_path_indices"] = [2]
        adapter.write_text(json.dumps(definition), encoding="utf-8")
        self.assertEqual(run_protocol(frozen, adapter, output, timeout_seconds=10)["runner"]["outcomes"], {"completed": 4})

    def test_exit_zero_does_not_imply_acceptance(self):
        frozen, adapter, output = self.runner_fixture('''import json,sys
request=json.load(sys.stdin)
print(json.dumps({"initial_state_sha256":request["task"]["dataset_sha256"]}))
''')
        run_protocol(frozen, adapter, output, timeout_seconds=10)
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertTrue(all(row["acceptance"]["status"] == "unknown" for row in rows))

    def test_timeout_and_nonzero_do_not_erase_attempts(self):
        frozen, adapter, output = self.runner_fixture('''import json,sys,time
request=json.load(sys.stdin)
if request["variant"]=="no_memory": time.sleep(2)
else: sys.exit(9)
''')
        report = run_protocol(frozen, adapter, output, timeout_seconds=.05)
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["wall_ms"] > 0 for row in rows))
        self.assertTrue(all(row["acceptance"]["status"] == "unknown" for row in rows))
        self.assertIn("timeout", report["runner"]["outcomes"])

    def test_adapter_cannot_replace_identity_or_claim_unprepared_dataset(self):
        frozen, adapter, output = self.runner_fixture('print(\'{"run_id":"override"}\')\n')
        report = run_protocol(frozen, adapter, output, timeout_seconds=10)
        self.assertEqual(report["runner"]["outcomes"], {"invalid_output": 4})
        output.unlink()
        (self.root / "adapter.py").write_text('print(\'{"initial_state_sha256":"wrong"}\')\n', encoding="utf-8")
        report = run_protocol(frozen, adapter, output, timeout_seconds=10)
        self.assertEqual(report["runner"]["outcomes"], {"dataset_unverified": 4})

    def test_malformed_adapter_enums_preserve_every_attempt_and_unknown_usage(self):
        frozen, adapter, output = self.runner_fixture('''import json,sys
request=json.load(sys.stdin)
acceptance={"method":[],"status":"unknown","evidence_sha256":None} if request["variant"]=="no_memory" else {"method":"automated","status":{},"evidence_sha256":None}
print(json.dumps({"initial_state_sha256":request["task"]["dataset_sha256"],"acceptance":acceptance}))
''')
        report = run_protocol(frozen, adapter, output, timeout_seconds=10)
        self.assertEqual(report["runner"]["outcomes"], {"invalid_observations": 4})
        rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row["run_id"] for row in rows}), 4)
        self.assertEqual([row["runner"]["schedule_position"] for row in rows], [1, 2, 3, 4])
        for row in rows:
            self.assertEqual(row["runner"]["status"], "invalid_observations")
            self.assertEqual(row["runner"]["exit_code"], 0)
            self.assertGreater(row["wall_ms"], 0)
            self.assertFalse(row["isolation"]["reset_verified"])
            self.assertEqual(row["acceptance"]["status"], "unknown")
            self.assertTrue(all(value is None for value in row["token_usage"].values()))
            self.assertIsNone(row["cost"])
        imported = report_files(frozen, output, self.root / "failed-attempts-report.json")
        self.assertEqual(imported["input_records"], 4)
        self.assertFalse(imported["invalid_records"])
        for arm in imported["all_reported_observations"].values():
            self.assertEqual(arm["runs"], 2)
            self.assertFalse(arm["token_usage"]["complete"])
            self.assertIsNone(arm["token_usage"]["total_tokens"])
            self.assertEqual(arm["cost"]["unknown_runs"], 2)

    def test_malformed_adapter_data_class_is_rejected_before_launch(self):
        frozen, adapter, output = self.runner_fixture('raise AssertionError("must not launch")\n')
        definition = json.loads(adapter.read_text(encoding="utf-8"))
        for value in ([], {}):
            definition["data_class"] = value
            adapter.write_text(json.dumps(definition), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(EvaluationError):
                run_protocol(frozen, adapter, output)
            self.assertFalse(output.exists())

    def test_snapshot_mismatch_and_synthetic_real_mix_rejected_before_launch(self):
        frozen, adapter, output = self.runner_fixture('raise AssertionError("must not launch")\n', real=True)
        adapter_json = json.loads(adapter.read_text())
        adapter_json["data_class"] = "synthetic_template"
        adapter.write_text(json.dumps(adapter_json), encoding="utf-8")
        with self.assertRaises(EvaluationError):
            run_protocol(frozen, adapter, output)
        self.assertFalse(output.exists())
        adapter_json["data_class"] = "real_adapter"
        adapter.write_text(json.dumps(adapter_json), encoding="utf-8")
        (self.root / "knowledge.json").write_text("changed", encoding="utf-8")
        with self.assertRaises(EvaluationError):
            run_protocol(frozen, adapter, output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
