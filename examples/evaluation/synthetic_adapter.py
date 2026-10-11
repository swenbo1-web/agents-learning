"""Stdlib runner smoke only: no agent, API, token measurement or savings claim."""
import hashlib
import json
import os
import time
from pathlib import Path


def main():
    import sys
    request = json.load(sys.stdin)
    workspace = Path(request["workspace"])
    if Path.cwd() != workspace or os.environ["ALP_HOME"] != request["alp_home"]:
        raise ValueError("isolated runner paths do not match")
    fixture = b'{"numbers":[2,3,5]}\n'
    (workspace / "fixture.json").write_bytes(fixture)
    dataset_sha256 = hashlib.sha256(fixture).hexdigest()
    if dataset_sha256 != request["task"]["dataset_sha256"]:
        raise ValueError("synthetic fixture differs from protocol")
    latencies = []
    if request["variant"] == "with_memory":
        started = time.perf_counter()
        knowledge = json.loads(Path(request["knowledge_snapshot_path"]).read_text(encoding="utf-8"))
        if knowledge.get("data_class") != "synthetic_template":
            raise ValueError("this adapter only handles synthetic snapshots")
        latencies.append((time.perf_counter() - started) * 1000)
    elif request["knowledge_snapshot_path"] is not None:
        raise ValueError("baseline must not receive a knowledge snapshot")
    result = sum(json.loads(fixture)["numbers"])
    evidence = json.dumps({"data_class": "synthetic_template", "result": result, "expected": 10}, sort_keys=True)
    print(json.dumps({"initial_state_sha256": dataset_sha256,
                      "acceptance": {"method": "automated", "status": "passed" if result == 10 else "failed",
                                     "evidence_sha256": hashlib.sha256(evidence.encode()).hexdigest()},
                      "retrieval": {"latencies_ms": latencies, "hit": None},
                      "token_usage": {name: None for name in ("worker", "admin", "retrieval", "model_other")},
                      "cost": None, "artifacts": []}))


if __name__ == "__main__":
    main()
