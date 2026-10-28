"""面向模型的有限知识响应；预算按完整 JSON 字符数计算，不裁切协议文本。"""
from __future__ import annotations

import json


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _length(value):
    return len(_encode(value))


def _budget(value, minimum, maximum):
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"max_chars must be an integer in {minimum}..{maximum}")
    return value


def _text(value):
    return value if isinstance(value, str) else ""


def _state(item):
    # 展示的来源版本与展示状态必须一起传给登记入口，不能另读一次猜基线。
    return {**({"source_files_changed": True} if item.get("source_files_changed") else {}),
            "source_health": item.get("source_health", "missing"),
            "maturity": item.get("maturity", "candidate"),
            "current_tool_success": bool(item.get("current_tool_success", False)),
            "validation_level": item.get("validation_level", "reported"),
            "knowledge_tier": item.get("knowledge_tier", "source_reference"),
            "source_version": item.get("source_version", -1),
            "pending_counterexample_count": item.get("pending_counterexample_count", 0),
            "feedback_status": item.get("feedback_status", "none"),
            "problem_changed": bool(item.get("problem_changed", any(
                source.get("problem_changed", False) for source in item.get("sources", []))))}


def _matched_conclusion_projection(item, limit):
    """One topic gate and original span for every conclusion rendering path."""
    from .knowledge_excerpt import conclusion_projection
    conclusion = conclusion_projection(_text(item.get("content")), limit)
    if conclusion is not None:
        matched = item.get("match", {}).get("matched_terms", [])
        if conclusion[0] and matched and not any(
                term.casefold() in conclusion[0].casefold() for term in matched if isinstance(term, str) and term):
            return "", 0, 0, True  # The topic occurred only in omitted background.
        return conclusion


def _summary_projection(item, limit=700):
    content = _text(item.get("content"))
    if len(content) <= limit:
        return content, 0, len(content), False
    conclusion = _matched_conclusion_projection(item, limit)
    if conclusion is not None:
        return conclusion
    matched = item.get("match", {}).get("matched_terms", [])
    lowered = content.lower()
    positions = [position for term in matched if isinstance(term, str) and term
                 if (position := lowered.find(term.lower())) >= 0]
    # 选取命中附近的原文，并明确标识摘录，不把首段当成全部解决方案。
    start = max(0, min(positions) - 100) if positions else 0
    end = min(len(content), start + limit - 2)
    return ("…" if start else "") + content[start:end] + "…", start, end, True


def _summary(item, limit=700):
    return _summary_projection(item, limit)[0]


def _shrink_summary(item, summary, limit):
    from .runtime import clip
    conclusion = _matched_conclusion_projection(item, limit)
    return conclusion[0] if conclusion is not None else clip(summary, limit)


def _card(item):
    match = item.get("match", {})
    result = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
              "title": _text(item.get("title"))[:160], "category": item.get("category", "unknown"),
              "knowledge_kind": item.get("knowledge_kind", "solution"),
              "applicability": _text(item.get("applicability"))[:240],
              "limitations": _text(item.get("limitations"))[:240],
              "content_summary": _summary(item), "summary_only": True,
              "match": {"score": match.get("score", 0), "coverage": match.get("coverage", 0),
                        "auto_eligible": bool(match.get("auto_eligible", False)),
                        "matched_terms": [term[:80] for term in match.get("matched_terms", [])[:12]
                                          if isinstance(term, str)]}, **_state(item)}
    result["truncated"] = (result["content_summary"] != item.get("content", "") or any(
        result[key] != item.get(key, "") for key in ("title", "applicability", "limitations")))
    result["read_required"] = any(result[key] != item.get(key, "") for key in ("applicability", "limitations"))
    if not result["content_summary"] and item.get("content"):
        result["read_required"] = True
    if match.get("method") in {"lexical", "semantic", "hybrid"}:
        result["match"]["method"] = match["method"]
    for key in ("semantic_score", "semantic_rank"):
        if key in match and isinstance(match[key], (int, float)):
            result["match"][key] = match[key]
    if match.get("fusion_method") in {"rrf", "reciprocal_rank_k60"}:
        result["match"]["fusion_method"] = match["fusion_method"]
    if "semantic_input_truncated" in match:
        result["match"]["semantic_input_truncated"] = bool(match["semantic_input_truncated"])
    return result


def _fit_card(card, maximum, item=None):
    if _length(card) <= maximum:
        return card
    card["truncated"] = True
    from .knowledge_excerpt import conclusion_range
    if item and conclusion_range(_text(item.get("content"))) is not None:
        # A complete conclusion and its boundaries are atomic. Manual search
        # can fall back to a preview, but cannot cut away a negation/condition.
        card["content_summary"] = ""
        card["read_required"] = True
        if _length(card) <= maximum:
            return card
    # 先减少正文摘录，再减少条件；无完整条件的卡仍明确声明只是预览。
    for key, minimum in (("content_summary", 0), ("applicability", 64), ("limitations", 64), ("title", 40)):
        original = card[key]
        if len(original) <= minimum:
            continue
        card[key] = original[:minimum]
        if key in {"applicability", "limitations"}:
            card["read_required"] = True
        if _length(card) <= maximum:
            low, high = minimum, len(original)
            while low < high:
                middle = (low + high + 1) // 2
                card[key] = original[:middle]
                if _length(card) <= maximum:
                    low = middle
                else:
                    high = middle - 1
            card[key] = original[:low]
            return card
    card["match"]["matched_terms"] = []
    return card if _length(card) <= maximum else None


def search_preview(result, max_chars=6000):
    """搜索只返回有限预览；省略的卡不会进入展示登记。"""
    max_chars = _budget(max_chars, 512, 20000)
    source_items = result.get("items", [])
    output = {key: result[key] for key in ("status", "generation", "revocation_epoch") if key in result}
    # Query safety survives the MCP preview boundary. Diagnostics contain only
    # bounded flags/counts, never the original prompt or arbitrary error text.
    for name, flags, counts in (
            ("query_diagnostics", ("input_truncated", "identity_scan_complete", "identity_limit_exceeded",
                                   "terms_truncated", "automatic_safe"), ()),
            ("recall_diagnostics", ("scan_limit_reached",), ("pages", "examined", "unreadable_records"))):
        supplied = result.get(name)
        if not isinstance(supplied, dict):
            continue
        diagnostics = {key: supplied[key] for key in flags if isinstance(supplied.get(key), bool)}
        for key in counts:
            value = supplied.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000:
                diagnostics[key] = value
        if diagnostics:
            output[name] = diagnostics
    if isinstance(result.get("retrieval"), dict):
        # 仅保留有限诊断，不把服务端响应或任意异常文本带入上下文。
        diagnostics = {}
        for key in ("mode", "semantic_status", "reason"):
            if isinstance(result["retrieval"].get(key), str):
                diagnostics[key] = result["retrieval"][key][:80]
        for key in ("retryable", "query_cache_hit", "query_truncated", "scan_truncated"):
            if isinstance(result["retrieval"].get(key), bool):
                diagnostics[key] = result["retrieval"][key]
        for key in ("retry_after_seconds", "scanned", "candidates", "invalid_vectors"):
            if isinstance(result["retrieval"].get(key), (int, float)):
                diagnostics[key] = result["retrieval"][key]
        if diagnostics:
            output["retrieval"] = diagnostics
    output.update(items=[], truncated=False, omitted_count=len(source_items))
    if _length(output) > max_chars:
        output.pop("retrieval", None)
    for index, item in enumerate(source_items):
        # 平分剩余空间，避免首条长正文耗尽所有卡片的预算。
        remaining = max_chars - _length(output) - 2
        share = remaining // max(1, len(source_items) - index)
        card = _fit_card(_card(item), share, item)
        if card is not None:
            output["items"].append(card)
            output["omitted_count"] -= 1
    output["truncated"] = bool(output["omitted_count"] or any(item["truncated"] for item in output["items"]))
    while _length(output) > max_chars and output["items"]:
        output["items"].pop()
        output["omitted_count"] += 1
        output["truncated"] = True
    return output


def read_page(item, max_chars=12000, offset=0, section="content"):
    """所选字段按精确偏移分页；完整约束优先保留，裁剪字段均可独立续读。"""
    max_chars = _budget(max_chars, 1024, 50000)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if section not in {"content", "applicability", "limitations"}:
        raise ValueError("section must be content, applicability or limitations")
    content = _text(item.get("content"))
    selected = _text(item.get(section))
    if offset > len(selected):
        raise ValueError("offset exceeds section length")
    sources = item.get("sources", [])
    output = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
              "title": _text(item.get("title")), "category": item.get("category", "unknown"),
              "knowledge_kind": item.get("knowledge_kind", "solution"),
              "applicability": _text(item.get("applicability")),
              "limitations": _text(item.get("limitations")), **_state(item),
              "metadata": {key: _text(value)[:180] for key, value in item.get("metadata", {}).items()},
              "sources": [{key: source[key] for key in ("candidate_id", "problem_id", "problem_revision",
                  "problem_changed", "health", "current_tool_success") if key in source}
                  for source in sources[:3]], "sources_count": len(sources),
              "sources_omitted_count": max(0, len(sources) - 3), "metadata_truncated": False,
              "fields_truncated": [], "section": section, "section_length": len(selected),
              "offset": offset, "content_length": len(content),
              "content": "", "next_offset": len(selected), "truncated": True}
    # 所选字段由下面的分页器填入，其他字段不能抢占它的正文空间。
    output[section] = ""
    output["metadata_truncated"] = output["metadata"] != item.get("metadata", {})

    def mark_fields():
        output["fields_truncated"] = [key for key in ("title", "applicability", "limitations")
                                      if output[key] != item.get(key, "")]

    mark_fields()
    # 为正文保留空间，摘要和来源的裁剪均显式标记。
    target = max_chars - 128
    while _length(output) > target and output["sources"]:
        output["sources"].pop()
        output["sources_omitted_count"] += 1
    if _length(output) > target and output["metadata"]:
        output["metadata"] = {}
        output["metadata_truncated"] = True
    for key in ("applicability", "limitations", "title"):
        if _length(output) <= target:
            break
        if key != section and len(output[key]) > 40:
            output[key] = output[key][:40]
            mark_fields()
    for key in ("applicability", "limitations", "title"):
        if _length(output) <= target:
            break
        if key != section and output[key]:
            output[key] = ""
            mark_fields()
    low, high = 0, len(selected) - offset
    while low < high:
        middle = (low + high + 1) // 2
        output[section] = selected[offset:offset + middle]
        output.update(next_offset=offset + middle, truncated=offset + middle < len(selected))
        mark_fields()
        if _length(output) <= max_chars:
            low = middle
        else:
            high = middle - 1
    end = offset + low
    output[section] = selected[offset:end]
    output.update(next_offset=end if end < len(selected) else None, truncated=end < len(selected))
    mark_fields()
    # 采用相同编码检查；EOF的null与数字长度不同也可能影响边界。
    while _length(output) > max_chars and output[section]:
        end -= 1
        output[section] = selected[offset:end]
        output.update(next_offset=end, truncated=True)
        mark_fields()
    if _length(output) > max_chars or (end == offset and offset < len(selected)):
        raise ValueError("max_chars cannot fit knowledge identity and a section page")
    return output
