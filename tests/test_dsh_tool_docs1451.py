"""Native DSH docs follow MCP without changing its existing tool permissions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from agent_learning.mcp import WORKER_TOOLS


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
COMMON_NAMES = {
    "knowledge_search", "knowledge_read", "work_memory_search", "work_memory_read",
    "session_context", "task_update", "case_feedback", "knowledge_use", "learning_status",
}
# Freeze the native permissions/validation contract separately from explanatory
# metadata. Its two native-only tools must not change as a side effect of docs.
NATIVE_BOUNDARY_SHA256 = "3c0e05291a6632ed400cc526482fc7f8cdf5ba7046dabdf6bdf29d065cf2a118"
READ_STATIC_SPECS = r"""
const fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const marker = 'const WORKER_TOOL_SPECS = ';
const start = source.indexOf(marker) + marker.length;
const end = source.indexOf('\n];', start) + 2;
if (start < marker.length || end < start) throw Error('native tool array missing');
// Evaluate only the static definitions, never apply(), hooks, or Python runners.
const specs = vm.runInNewContext('(' + source.slice(start, end) + ')', {}, {timeout:1000});
process.stdout.write(JSON.stringify(specs));
"""


def _without_docs(value):
    if isinstance(value, dict):
        return {key: _without_docs(item) for key, item in value.items()
                if key not in {"description", "default"}}
    if isinstance(value, list):
        return [_without_docs(item) for item in value]
    return value


def _metadata(schema):
    return {key: schema[key] for key in ("description", "default") if key in schema}


@unittest.skipUnless(NODE, "Node.js required to inspect native DSH definitions")
class DSHWorkerToolDocs1451Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [NODE, "-e", READ_STATIC_SPECS, str(ROOT / "host_resources/dsh/host/index.mjs")],
            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.native_list = json.loads(result.stdout)
        cls.native = {spec["name"]: spec for spec in cls.native_list}
        cls.mcp = {name: (description, contract) for name, description, contract in WORKER_TOOLS}

    def test_native_tool_set_keeps_its_existing_surface(self):
        self.assertEqual(set(self.native), COMMON_NAMES | {"learning_history", "learning_browser"})
        self.assertEqual(len(self.native_list), 11)
        self.assertNotIn("candidate_reference_read", self.native)
        self.assertNotIn("candidate_reference_feedback", self.native)
        self.assertFalse(set(self.native) & {"publish", "revoke", "submit_candidate"})

    def test_native_permissions_and_validation_contract_are_unchanged(self):
        boundary = [{"name": spec["name"], "parameters": _without_docs(spec["parameters"]),
                     "readOnly": spec["readOnly"]} for spec in self.native_list]
        serialized = json.dumps(boundary, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(serialized.encode()).hexdigest(), NATIVE_BOUNDARY_SHA256)

    def test_common_tool_descriptions_match_mcp(self):
        for name in COMMON_NAMES:
            with self.subTest(tool=name):
                self.assertEqual(self.native[name]["description"], self.mcp[name][0])

    def test_common_parameter_docs_and_defaults_match_mcp(self):
        def compare(actual, expected, path):
            self.assertEqual(_metadata(actual), _metadata(expected), path)
            for key, value in actual.get("properties", {}).items():
                self.assertIn(key, expected.get("properties", {}), path + "." + key)
                compare(value, expected["properties"][key], path + "." + key)
            if "items" in actual and isinstance(actual["items"], dict):
                compare(actual["items"], expected["items"], path + "[]")

        for name in COMMON_NAMES:
            with self.subTest(tool=name):
                compare(self.native[name]["parameters"], self.mcp[name][1], name)

    def test_every_common_parameter_has_explanatory_text(self):
        for name in COMMON_NAMES:
            for key, contract in self.native[name]["parameters"]["properties"].items():
                with self.subTest(tool=name, parameter=key):
                    self.assertTrue(contract.get("description", "").strip())


if __name__ == "__main__":
    unittest.main()
