"""Connection-local receipts for returned work sections; no evidence text is cached."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


class WorkReadDelivery:
    MAX_RECEIPTS = 128
    MAX_RANGES = 16

    def __init__(self):
        self.receipts = OrderedDict()

    def clear(self):
        self.receipts.clear()

    def project(self, result, scope, *, reread=False):
        """Called after live access/source checks and successful exposure bookkeeping."""
        if (scope is None or result.get("status") != "ok"
                or result.get("section") not in {"summary", "evidence"}):
            return result
        length, offset, text = result.get("section_length"), result.get("offset"), result.get("content")
        if (type(length) is not int or type(offset) is not int or not isinstance(text, str)
                or not 0 <= offset <= offset + len(text) <= length):
            return result
        key = _hash([scope, result["memory_id"], result["section"]])
        fingerprint = _hash({key: result.get(key) for key in (
            "revision", "source_version", "state", "completion", "source_changed",
            "candidate_reference_risk", "file_state", "section_length")})
        previous = self.receipts.pop(key, None)
        ranges = previous[1] if previous and previous[0] == fingerprint else []
        complete = ranges == [(0, length)]
        # An already-returned receipt is not proof the model read or remembered it.
        if complete and not reread:
            self.receipts[key] = (fingerprint, ranges)
            return {"status": "ok", "memory_id": result["memory_id"], "revision": result["revision"],
                "source_version": result["source_version"], "state": result["state"],
                "source_changed": result["source_changed"], "validation_level": result["validation_level"],
                "section": result["section"], "section_length": length, "offset": offset,
                "next_offset": None, "truncated": False, "content_omitted": True,
                "already_read": True, "section_previously_returned_complete": True,
                "model_read_confirmed": False, "execution_authority": False,
                "reread_hint": "Reuse the previously returned section, or set reread=true to return it again."}
        merged = []
        for start, end in sorted(ranges + [(offset, offset + len(text))]):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        # Dropping excess disjoint ranges can only cause another full read, never
        # turn partial output into a complete receipt.
        self.receipts[key] = (fingerprint, merged[:self.MAX_RANGES])
        while len(self.receipts) > self.MAX_RECEIPTS:
            self.receipts.popitem(last=False)
        return result
