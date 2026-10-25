"""Connection-local receipts for returned work sections; no evidence text is cached."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def coverage_limits(coverage):
    """Keep restrictive original-coverage facts when presentation details shrink."""
    if not isinstance(coverage, dict):
        return {}
    limits = {key: coverage[key] for key, value in (
        ("truncated", True), ("full_original_available", False), ("file_projection_complete", False))
        if coverage.get(key) is value}
    if isinstance(coverage.get("segments"), int) and coverage["segments"] > 1:
        limits.update({key: coverage[key] for key in ("segment", "segments") if key in coverage})
    return limits


class WorkReadDelivery:
    MAX_RECEIPTS = 128
    MAX_RANGES = 16

    def __init__(self):
        self.receipts = OrderedDict()

    def clear(self):
        self.receipts.clear()

    def completed(self, result, scope, *, section, offset, max_chars):
        """Reuse only after the caller has rechecked current access, source and files."""
        from .runtime import ContractError
        if scope is None:
            return None
        key = _hash([scope, result["memory_id"], section])
        previous = self.receipts.get(key)
        if previous is None or previous[1] != [(0, previous[2])]:
            return None
        candidate = dict(result, status="ok", section=section, section_length=previous[2], offset=offset)
        check = candidate.get("file_check")
        if not isinstance(check, dict) or check.get("complete") is not True or self._fingerprint(candidate) != previous[0]:
            return None
        if offset > previous[2]:
            raise ContractError("offset exceeds work section length")
        receipt = self._receipt(candidate)
        if len(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) > max_chars:
            return None
        self.receipts.move_to_end(key)
        return receipt

    @staticmethod
    def _fingerprint(result):
        return _hash({key: result.get(key) for key in (
            "revision", "source_version", "state", "completion", "source_changed",
            "file_check", "section_length")})

    @staticmethod
    def _receipt(result):
        # Returning a complete section is distinct from having a complete
        # original observation. Keep restrictive flags on both receipt paths.
        limits = {}
        if result.get("file_projection_incomplete"):
            limits.update(file_projection_incomplete=True, automatic_eligible=False)
        if result.get("automatic_eligible") is False:
            limits["automatic_eligible"] = False
        if result.get("completion") not in (None, "complete"):
            limits["completion"] = result["completion"]
        restricted = coverage_limits(result.get("coverage"))
        if restricted:
            limits["coverage"] = restricted
        return {"status": "ok", "memory_id": result["memory_id"], "revision": result["revision"],
            "source_version": result["source_version"], "state": result["state"],
            "source_changed": result["source_changed"], "validation_level": result["validation_level"],
            "file_check": result["file_check"],
            **({"candidate_reference_risk": [{"blocked": True, "details_omitted": True}],
                "automatic_eligible": False} if result.get("candidate_reference_risk") else {}),
            **limits,
            "section": result["section"], "section_length": result["section_length"], "offset": result["offset"],
            "next_offset": None, "truncated": False, "content_omitted": True,
            "already_read": True, "section_previously_returned_complete": True,
            "model_read_confirmed": False, "execution_authority": False,
            "reread_hint": "Reuse the previously returned section, or set reread=true to return it again."}

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
        file_check = result.get("file_check")
        if not isinstance(file_check, dict) or file_check.get("complete") is not True:
            # Unknown or over-budget file observations cannot establish that a
            # previous full section still describes the same available state.
            self.receipts.pop(key, None)
            return result
        fingerprint = self._fingerprint(result)
        previous = self.receipts.pop(key, None)
        ranges = previous[1] if previous and previous[0] == fingerprint else []
        complete = ranges == [(0, length)]
        if reread and (complete or offset == 0):
            # A new replay must cover its own ranges. Explicit reread on each
            # continuation page remains compatible, without resetting that round.
            ranges = []
            complete = False
        # An already-returned receipt is not proof the model read or remembered it.
        if complete and not reread:
            self.receipts[key] = (fingerprint, ranges, length)
            return self._receipt(result)
        merged = []
        for start, end in sorted(ranges + [(offset, offset + len(text))]):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        # Dropping excess disjoint ranges can only cause another full read, never
        # turn partial output into a complete receipt.
        self.receipts[key] = (fingerprint, merged[:self.MAX_RANGES], length)
        while len(self.receipts) > self.MAX_RECEIPTS:
            self.receipts.popitem(last=False)
        return result
