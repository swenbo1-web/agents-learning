"""Small technical vocabulary, not translation or a semantic model."""
from __future__ import annotations

import re
from functools import lru_cache


LANGUAGE_VERSION = 1
ALIASES = {
    "administrator": ("administrator", "administrators", "admin", "管理员"),
    "project": ("project", "projects", "项目"),
    "history": ("history", "historical", "历史"),
    "review": ("review", "reviews", "审核", "审查"),
    "summary": ("summary", "summaries", "摘要"),
    "knowledge": ("knowledge", "知识"),
    "memory": ("memory", "记忆"),
    "revision": ("revision", "revisions", "修订"),
    "revocation": ("revocation", "revoke", "revoked", "撤销"),
    "exposure": ("exposure", "展示"),
    "evidence": ("evidence", "证据"),
    "public": ("public", "公开"),
    "work": ("work", "工作"),
    "budget": ("budget", "budgets", "预算"),
    "unknown": ("unknown", "未知"),
    "frozen": ("frozen", "冻结"),
    "batch": ("batch", "batches", "批次"),
    "incremental": ("incremental", "增量"),
    "global": ("global", "全局"),
    "index": ("index", "索引"),
    "detail": ("detail", "正文", "详情"),
    "progressive": ("progressive", "渐进式", "渐进"),
    "paging": ("paging", "pagination", "分页"),
    "memoryleak": ("memory leak", "memory leaks", "内存泄漏"),
    "deadlock": ("deadlock", "deadlocks", "死锁"),
    "permissiondenied": ("permission denied", "权限不足", "拒绝访问"),
    "dependency": ("dependency", "dependencies", "依赖"),
    "rollback": ("rollback", "回滚"),
    "timeout": ("timeout", "超时"),
    "cache": ("cache", "缓存"),
}
_LOOKUP = {alias: key for key, aliases in ALIASES.items() for alias in aliases}
_PARTS = []
for _alias in sorted(_LOOKUP, key=lambda item: (-len(item), item)):
    # Do not translate substrings inside paths, error codes or configuration keys.
    _escaped = re.escape(_alias).replace(r"\ ", r"\s+")
    _PARTS.append(_escaped if re.search(r"[\u3400-\u9fff]", _alias) else
                  r"(?<![a-z0-9_./:\\-])" + _escaped + r"(?![a-z0-9_./:\\-])")
_PATTERN = re.compile("|".join(_PARTS), re.IGNORECASE)


def _rewrite(text):
    return _PATTERN.sub(lambda match: " " + _LOOKUP[re.sub(r"\s+", " ", match[0].lower())] + " ", text)


@lru_cache(maxsize=128)
def _short_text(text):
    return _rewrite(text)


def canonical_text(text):
    # Shared short conditions occur across many cards. Keep the in-process
    # cache bounded in both entry count and input size; long bodies aren't kept.
    return _short_text(text) if len(text) <= 768 else _rewrite(text)


def recall_terms(selected, tokenizer):
    result = list(selected)
    for key in selected:
        if key in ALIASES:
            result.extend(word for word in tokenizer(" ".join(ALIASES[key]))
                          if not re.fullmatch(r"[\u3400-\u9fff]", word))
    return list(dict.fromkeys(result))[:128]


def matched_labels(query, matched, tokenizer):
    aliases, translated = [], set()
    for match in _PATTERN.finditer(query):
        key = _LOOKUP[re.sub(r"\s+", " ", match[0].lower())]
        if key in matched:
            translated.add(key)
            aliases.extend(word for word in tokenizer(match[0]) if len(word) > 1)
    return list(dict.fromkeys([word for word in matched if word not in translated] + aliases))[:12]
