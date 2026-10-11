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


class ProtocolTests(unittest.TestCase):
    def test_protocol_is_stable_and_order_balanced(self):
        first = create_protocol(spec())
        self.assertEqual(first, create_protocol(spec()))
        firsts = [first["execution_order"][index]["variant"] for index in (0, 2)]
        self.assertEqual(set(firsts), {"no_memory", "with_memory"})

    def test_require_multiple_repeats_and_explicit_source(self):
        for key, value in (("repeats", 1), ("data_class", None), ("seed", True)):
            definition = spec()
            definition[key] = value
            with self.assertRaises(EvaluationError):
                create_protocol(definition)

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


class FileAndRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, content):
        path = self.root / name
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

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
