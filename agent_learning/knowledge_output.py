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
    return {"source_health": item.get("source_health", "missing"),
            "current_tool_success": bool(item.get("current_tool_success", False)),
            "validation_level": item.get("validation_level", "reported"),
            "source_version": item.get("source_version", -1),
            "pending_counterexample_count": item.get("pending_counterexample_count", 0),
            "feedback_status": item.get("feedback_status", "none"),
            "problem_changed": bool(item.get("problem_changed", any(
                source.get("problem_changed", False) for source in item.get("sources", []))))}


def _summary(item, limit=700):
    content = _text(item.get("content"))
    if len(content) <= limit:
        return content
    matched = item.get("match", {}).get("matched_terms", [])
    positions = [content.lower().find(term.lower()) for term in matched
                 if isinstance(term, str) and term and content.lower().find(term.lower()) >= 0]
    # 选取命中附近的原文，并明确标识摘录，不把首段当成全部解决方案。
    start = max(0, min(positions) - 100) if positions else 0
    return ("…" if start else "") + content[start:start + limit - 2] + "…"


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
    return result


def _fit_card(card, maximum):
    if _length(card) <= maximum:
        return card
    card["truncated"] = True
    # 先减少正文摘录，再减少条件；无完整条件的卡仍明确声明只是预览。
    for key, minimum in (("content_summary", 0), ("applicability", 64), ("limitations", 64), ("title", 40)):
        original = card[key]
        if len(original) <= minimum:
            continue
        card[key] = original[:minimum]
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
    output.update(items=[], truncated=False, omitted_count=len(source_items))
    for index, item in enumerate(source_items):
        # 平分剩余空间，避免首条长正文耗尽所有卡片的预算。
        remaining = max_chars - _length(output) - 2
        share = remaining // max(1, len(source_items) - index)
        card = _fit_card(_card(item), share)
        if card is not None:
            output["items"].append(card)
            output["omitted_count"] -= 1
    output["truncated"] = bool(output["omitted_count"] or any(item["truncated"] for item in output["items"]))
    while _length(output) > max_chars and output["items"]:
        output["items"].pop()
        output["omitted_count"] += 1
        output["truncated"] = True
    return output


def read_page(item, max_chars=12000, offset=0):
    """分页正文保留精确字符偏移；来源仅列有限状态，不复制原始证据。"""
    max_chars = _budget(max_chars, 1024, 50000)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    content = _text(item.get("content"))
    if offset > len(content):
        raise ValueError("offset exceeds content length")
    sources = item.get("sources", [])
    output = {"knowledge_id": item["knowledge_id"], "revision": item["revision"],
              "title": _text(item.get("title"))[:160], "category": item.get("category", "unknown"),
              "knowledge_kind": item.get("knowledge_kind", "solution"),
              "applicability": _text(item.get("applicability"))[:240],
              "limitations": _text(item.get("limitations"))[:240], **_state(item),
              "metadata": {key: _text(value)[:180] for key, value in item.get("metadata", {}).items()},
              "sources": [{key: source[key] for key in ("candidate_id", "problem_id", "problem_revision",
                  "problem_changed", "health", "current_tool_success") if key in source}
                  for source in sources[:3]], "sources_count": len(sources),
              "sources_omitted_count": max(0, len(sources) - 3), "metadata_truncated": False,
              "fields_truncated": [], "offset": offset, "content_length": len(content),
              "content": "", "next_offset": len(content), "truncated": True}
    output["metadata_truncated"] = output["metadata"] != item.get("metadata", {})
    output["fields_truncated"] = [key for key in ("title", "applicability", "limitations")
                                  if output[key] != item.get(key, "")]
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
        if len(output[key]) > 40:
            output[key] = output[key][:40]
            if key not in output["fields_truncated"]:
                output["fields_truncated"].append(key)
    low, high = 0, len(content) - offset
    while low < high:
        middle = (low + high + 1) // 2
        output.update(content=content[offset:offset + middle], next_offset=offset + middle,
                      truncated=offset + middle < len(content))
        if _length(output) <= max_chars:
            low = middle
        else:
            high = middle - 1
    end = offset + low
    output.update(content=content[offset:end], next_offset=end if end < len(content) else None,
                  truncated=end < len(content))
    # 采用相同编码检查；EOF的null与数字长度不同也可能影响边界。
    while _length(output) > max_chars and output["content"]:
        end -= 1
        output.update(content=content[offset:end], next_offset=end, truncated=True)
    if _length(output) > max_chars or (end == offset and offset < len(content)):
        raise ValueError("max_chars cannot fit knowledge identity and a content page")
    return output
