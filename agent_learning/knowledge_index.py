"""可重建的 active 知识投影；正文、修订和来源仍由 SQLite 权威表保存。"""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from .query_language import LANGUAGE_VERSION, canonical_text, recall_terms, matched_labels
from .semantic_retrieval import SemanticRetrievalMixin


INDEX_VERSION = 6
_WORD_PART = r"[\w\u0300-\u036f]+"
_WORD_TAIL = r"(?:(?:[./:-]|:/)\.?" + _WORD_PART + r")"
# Keep the complete technical identity before emitting searchable components.
# Plain Chinese prose still uses the existing bigram/character lane. A dotfile
# or a Unicode path must never silently become just its ASCII basename.
_WORDS = re.compile("|".join((r"c\+\+(?:[0-9]+)?", r"c#",
    r"(?:\.{1,2}/)+\.?" + _WORD_PART + _WORD_TAIL + "*",
    r"\." + _WORD_PART + _WORD_TAIL + "*",
    _WORD_PART + _WORD_TAIL + "+", r"[a-z0-9_]+", r"[\u3400-\u9fff]+")))
_CHINESE = re.compile(r"[\u3400-\u9fff]+\Z")
_FIELD_WEIGHTS = {"title": 4, "content": 2, "applicability": 2, "limitations": 1,
                  "category": 1, "knowledge_kind": 1}
_EN_REQUEST_WORDS = {"please", "help", "find", "check", "inspect", "explain", "analyze", "analyse",
                     "detail", "details", "carefully", "first", "then", "finally", "how", "what", "why",
                     "the", "a", "an", "to", "of", "for", "in", "on", "and", "or", "is", "are", "can",
                     "could", "would", "should", "this", "that", "with", "my", "our", "me", "we"}
_ZH_REQUEST = re.compile("|".join(sorted({"请先", "帮我", "请问", "一下", "认真", "深度", "详细", "仔细",
                        "查找", "查询", "检查", "看看", "查看", "解决", "分析", "如何", "怎么", "怎样",
                        "问题", "这个", "那个", "这些", "我们", "现在", "目前", "相关", "内容", "方法",
                        "说明", "给出", "进行", "可以", "需要", "是否", "能够", "然后", "最后", "请"},
                       key=lambda value: (-len(value), value))))
_MAX_QUERY_TERMS = 128
_LEXICAL_CANDIDATES = 128
_MAX_RECALL_PAGES = 4
_MAX_QUERY_CHARS = 65536
_MATCH_POLICY_VERSION = 4
_RECALL_POLICY_VERSION = 1
_TOOL_FIELDS = {"exit_code", "return_code", "returncode", "is_error", "tool_response", "tool_name"}
_ZH_CONTEXT = re.compile("|".join(sorted({"管理员设置", "设置页面", "页面设置", "管理页面", "项目设置",
                        "当前项目", "本地项目", "项目内容", "工作流程", "执行流程", "切换模型",
                        "模型设置", "按钮样式", "主题颜色"}, key=lambda value: (-len(value), value))))


def _exact_anchors(query):
    """Keep full identifiers; fragments such as ``auth`` cannot stand for an error code."""
    result = []
    for word in _WORDS.findall(query.lower().replace("\\", "/")):
        if (_CHINESE.fullmatch(word) or word in _TOOL_FIELDS
                or re.fullmatch(r"\d+(?:\.\d+)*", word)):
            continue
        if (any(char in word for char in "_./:") or re.search(r"\d", word)
                or word.startswith("c++") or word == "c#"):
            result.append(terms(word)[0])
    result = list(dict.fromkeys(result))
    # Counts and language/version background must not veto an exact issue or
    # file identity. Explicit environment compatibility is checked separately.
    specific = [anchor for anchor in result if _specific_anchor(anchor)]
    return specific or result


def _specific_anchor(anchor):
    # A full path, file or error/setting identifier can locate one concrete issue.
    # Language names, a bare number or a version alone still need normal coverage.
    return ("_" in anchor or "/" in anchor or ":" in anchor
            or "." in anchor and not re.fullmatch(r"v?\d+(?:\.\d+)+", anchor)
            or re.fullmatch(r"(?:e|err|error)\d+[a-z0-9]*|0x[0-9a-f]+|[a-z][a-z0-9]*-\d[a-z0-9_-]*",
                            anchor) is not None)


def terms(text):
    """索引与评分共用词元；技术标识同时保留整体及其可独立检索的组件。"""
    result = []
    for word in _WORDS.findall(text.lower().replace("\\", "/")):
        if word.startswith("c++"):
            result.append("cplusplus")
            if word[3:]:
                result.append("cplusplus" + word[3:])
        elif word == "c#":
            result.append("csharp")
        elif _CHINESE.fullmatch(word):
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
            result.extend(word)
        else:
            result.append(word)
            if re.search(r"[./:_-]", word):
                for component in re.split(r"[./:_-]+", word):
                    if _CHINESE.fullmatch(component):
                        result.extend(component[i:i + 2] for i in range(len(component) - 1))
                        result.extend(component)
                    elif component:
                        result.append(component)
    return list(dict.fromkeys(result))


tokenize = terms


def _query_terms(query, anchors=()):
    # 请求修饰语不参与相关度；先保留双字和标识，再限制总量，避免单字占满预算。
    meaningful = _ZH_REQUEST.sub(" ", query[-8000:])
    selected = [word for word in match_terms(meaningful) if word not in _EN_REQUEST_WORDS]
    if any(_CHINESE.fullmatch(word) and len(word) > 1 for word in selected):
        selected = [word for word in selected if not (_CHINESE.fullmatch(word) and len(word) == 1)]
    # 技术标识通常比自然语言连接片段明确，超预算时优先保留。
    technical = [word for word in selected if not _CHINESE.fullmatch(word)]
    chinese = [word for word in selected if _CHINESE.fullmatch(word)]
    ordered = list(dict.fromkeys(technical + chinese))
    complete = list(dict.fromkeys(list(anchors) + ordered))
    preferred = list(dict.fromkeys(list(anchors) + technical))[:_MAX_QUERY_TERMS]
    remaining = _MAX_QUERY_TERMS - len(preferred)
    if len(chinese) > remaining and remaining:
        # Natural-language problems often appear after background. Keep both
        # ends while the full question remains available to phrase validation.
        head = (remaining + 1) // 2
        chinese = chinese[:head] + (chinese[-(remaining - head):] if remaining > head else [])
    keep = set((preferred + chinese)[:_MAX_QUERY_TERMS])
    return [word for word in dict.fromkeys(ordered + list(anchors)) if word in keep], meaningful, len(complete) > _MAX_QUERY_TERMS


def normalized_query_plan(query):
    """缓存和检索共享同一有效输入；短语顺序也参与自动匹配判断。"""
    # Scoring is bounded, but a forgotten error/path must never become permission
    # to inject unrelated knowledge. Scan a separate bounded original-input lane;
    # if that lane cannot represent the complete identity, only manual recall is
    # allowed. No model or larger context budget is required.
    scanned = query[:_MAX_QUERY_CHARS]
    complete_anchors = _exact_anchors(scanned)
    complete_anchors.sort(key=lambda anchor: bool("/" in anchor or "." in anchor))
    anchors = complete_anchors[:_MAX_QUERY_TERMS]
    selected, meaningful, terms_truncated = _query_terms(query, anchors)
    diagnostics = {"input_truncated": len(query) > 8000,
                   "identity_scan_complete": len(query) <= _MAX_QUERY_CHARS,
                   "identity_limit_exceeded": len(complete_anchors) > _MAX_QUERY_TERMS,
                   "terms_truncated": terms_truncated}
    automatic_safe = (diagnostics["identity_scan_complete"] and not diagnostics["identity_limit_exceeded"]
                      and (bool(anchors) or not diagnostics["input_truncated"]))
    diagnostics["automatic_safe"] = automatic_safe
    return {"terms": selected, "meaningful": meaningful,
            "recall_terms": recall_terms(selected, terms), "language_version": LANGUAGE_VERSION,
            "exact_anchors": anchors, "automatic_safe": automatic_safe,
            "query_diagnostics": diagnostics,
            "input_fingerprint": hashlib.sha256(query.encode("utf-8", errors="surrogatepass")).hexdigest()
                                 if any(diagnostics[key] for key in ("input_truncated", "terms_truncated")) else None,
            "match_policy_version": _MATCH_POLICY_VERSION,
            "recall_policy_version": _RECALL_POLICY_VERSION}


def match_terms(text):
    """Normalize meanings for scoring; stored FTS tokens stay backward compatible."""
    return terms(canonical_text(text))


def _index_text(body):
    parts = [body.get(field, "solution" if field == "knowledge_kind" else "")
             for field in _FIELD_WEIGHTS]
    parts.extend(body.get("metadata", {}).values())
    return " ".join(terms(" ".join(value for value in parts if isinstance(value, str))))


def _match_values(body_json, query_weights, denominator):
    """查询覆盖优先；每个词只取最高字段权重，重复字段不能累加刷高分。"""
    body = json.loads(body_json)
    matches = {}
    for field, weight in _FIELD_WEIGHTS.items():
        value = body.get(field, "solution" if field == "knowledge_kind" else "")
        if isinstance(value, str):
            for token in match_terms(value):
                if token in query_weights:
                    matches[token] = max(weight, matches.get(token, 0))
    values = body.get("metadata", {}).values()
    for token in match_terms(" ".join(value for value in values if isinstance(value, str))):
        if token in query_weights:
            matches[token] = max(2, matches.get(token, 0))
    coverage = sum(query_weights[token] for token in matches) / denominator
    field_score = sum(query_weights[token] * weight for token, weight in matches.items()) / denominator
    # 查询最多128词且权重最多2，因此相差一个词的覆盖增益始终大于字段增益。
    return coverage * 2048 + field_score, coverage, matches


def _rank(body_json, query_weights, denominator):
    """只使用当前知识和查询，其他项目、历史条目和词频不影响排名。"""
    return _match_values(body_json, query_weights, denominator)[0]


def _positive_anchors_allowed(body_json, anchors):
    if not anchors:
        return True
    body = json.loads(body_json)
    fields = [body.get(field, "") for field in ("title", "content", "applicability")]
    fields.extend(body.get("metadata", {}).values())
    positive = set(match_terms(" ".join(value for value in fields if isinstance(value, str))))
    # A restriction saying "not applicable to E_IMPORT" is not a positive
    # identity match and must not grant the sparse-query or semantic shortcut.
    return all(anchor in positive for anchor in anchors)


def _match_info(body_json, query_weights, denominator, query, *, _values=None, _anchors=None):
    # 独立于 _rank，缓存命中仍生成可解释字段，但不重排已经固定的修订引用。
    score, coverage, matches = (_match_values(body_json, query_weights, denominator)
                                if _values is None else _values)
    strong = [token for token in matches if len(token) > 1]
    anchors = ([word for word in _exact_anchors(query) if word in query_weights]
               if _anchors is None else _anchors)
    exact = _positive_anchors_allowed(body_json, anchors)
    eligible = exact and bool(strong) and (coverage >= 0.65 or any(
        _specific_anchor(anchor) for anchor in anchors))
    if not eligible and strong:
        # 长中文请求会降低全句覆盖；连续四字的问题短语仍可作为明确命中依据。
        body = json.loads(body_json)
        positive = " ".join(body.get(key, "") for key in ("title", "content", "applicability")
                            if isinstance(body.get(key, ""), str))
        for segment in re.findall(r"[\u3400-\u9fff]+", _ZH_CONTEXT.sub(" ", query)):
            for index in range(len(segment) - 3):
                if exact and segment[index:index + 4] in positive:
                    eligible = True
                    break
            if eligible:
                break
    return {"score": round(score, 6), "coverage": round(coverage, 6),
            "matched_terms": matched_labels(query, [token for token in query_weights if token in matches], terms),
            "auto_eligible": eligible}


def lexical_rows(db, project, plan, filters=None, *, page=0, diagnostics=None):
    """Bound FTS recall without installing per-query SQLite functions.

    Manual queries retain the broad lane. Automatic queries that require every
    identifier only scan the AND lane: broad-only rows cannot pass the existing
    positive-identifier gate. Keep paging that lane to find healthy later rows.
    """
    filters = filters or {}
    conditions = _filter_sql(filters)
    base = ("SELECT k.* FROM knowledge_fts JOIN active a ON "
            "knowledge_fts.project=a.project AND knowledge_fts.id=a.id AND "
            "CAST(knowledge_fts.revision AS INTEGER)=a.revision JOIN knowledge k ON "
            "k.project=a.project AND k.id=a.id AND k.revision=a.revision "
            "WHERE knowledge_fts MATCH ? AND a.project=?" + conditions +
            " ORDER BY bm25(knowledge_fts),k.id LIMIT ? OFFSET ?")
    quote = lambda word: '"' + word.replace('"', '""') + '"'
    anchors = plan.get("exact_anchors", [])
    lanes = []
    if anchors:
        lanes.append((" AND ".join(quote(word) for word in anchors), _LEXICAL_CANDIDATES // 2))
    if not (anchors and plan.get("required_anchor_recall")):
        lanes.append((" OR ".join(quote(word) for word in plan["recall_terms"]),
                      _LEXICAL_CANDIDATES // 2 if anchors else _LEXICAL_CANDIDATES))
    rows = {}
    for expression, maximum in lanes:
        if expression:
            found = db.execute(base, (expression, project, *filters.values(), maximum, page * maximum)).fetchall()
            if diagnostics is not None and len(found) == maximum:
                diagnostics["page_may_have_more"] = True
            for row in found:
                rows[row["id"]] = row
    return list(rows.values())


def _filter_sql(filters):
    # 字段已经过 knowledge_filters 白名单校验；值始终绑定为参数。
    conditions = []
    for field in filters:
        if field == "knowledge_kind":
            conditions.append(" AND COALESCE(CASE WHEN json_valid(k.body) THEN "
                              "json_extract(k.body,'$.knowledge_kind') END,'solution')=?")
        elif field == "category":
            conditions.append(" AND CASE WHEN json_valid(k.body) THEN json_extract(k.body,'$.category') END=?")
        else:
            conditions.append(" AND CASE WHEN json_valid(k.body) THEN json_extract(k.body,'$.metadata." + field + "') END=?")
    return "".join(conditions)


class KnowledgeIndexMixin(SemanticRetrievalMixin):
    def _index_knowledge(self, project, knowledge_id, revision, body):
        from .runtime import clean

        self._remove_knowledge_index(project, knowledge_id)
        inserted = self.db.execute("INSERT INTO knowledge_fts VALUES(?,?,?,?)",
                                   (project, knowledge_id, revision, _index_text(clean(body))))
        self.db.execute("INSERT INTO knowledge_index_entries VALUES(?,?,?,?)",
                        (project, knowledge_id, revision, inserted.lastrowid))

    def _remove_knowledge_index(self, project, knowledge_id):
        row = self.db.execute("SELECT fts_rowid FROM knowledge_index_entries WHERE project=? AND id=?",
                              (project, knowledge_id)).fetchone()
        if row:
            self.db.execute("DELETE FROM knowledge_fts WHERE rowid=?", (row[0],))
            self.db.execute("DELETE FROM knowledge_index_entries WHERE project=? AND id=?", (project, knowledge_id))

    def _rebuild_knowledge_index(self):
        # 调用方持有写事务，投影、版本和旧缓存同时切换。
        self.db.execute("CREATE TABLE IF NOT EXISTS knowledge_index_state "
                        "(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS knowledge_index_entries "
                        "(project TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,"
                        "fts_rowid INTEGER NOT NULL UNIQUE,PRIMARY KEY(project,id))")
        self.db.execute("DELETE FROM knowledge_fts")
        self.db.execute("DELETE FROM knowledge_index_entries")
        rows = self.db.execute("SELECT k.project,k.id,k.revision,k.body FROM knowledge k JOIN active a "
                               "ON k.project=a.project AND k.id=a.id AND k.revision=a.revision")
        count = 0
        for row in rows:
            self._index_knowledge(row["project"], row["id"], row["revision"], json.loads(row["body"]))
            count += 1
        self.db.execute("INSERT INTO knowledge_index_state VALUES(1,?) "
                        "ON CONFLICT(id) DO UPDATE SET version=excluded.version", (INDEX_VERSION,))
        self.db.execute("DELETE FROM cache")
        return count

    def rebuild_index(self):
        if self.db.in_transaction:
            count = self._rebuild_knowledge_index()
        else:
            with self.transaction():
                count = self._rebuild_knowledge_index()
        return {"status": "rebuilt", "indexed": count, "index_version": INDEX_VERSION}

    def search(self, project, query, limit=5, filters=None, *, _automatic=False, _environment=None):
        from .runtime import ContractError, digest, encode, knowledge_filters

        if not isinstance(query, str):
            raise ContractError("query must be a string")
        limit = max(1, min(int(limit), 10))
        filters = knowledge_filters(filters)
        plan = normalized_query_plan(query)
        recall_plan = dict(plan, required_anchor_recall=bool(_automatic and plan["exact_anchors"]))
        selected, meaningful = plan["terms"], plan["meaningful"]
        if not selected or (_automatic and not plan["automatic_safe"]):
            return {"status": "no_match", "items": [], "query_diagnostics": plan["query_diagnostics"]}

        # Prepare the one query embedding before acquiring the SQLite snapshot. Index
        # construction remains an explicit operation, never part of a prompt query.
        semantic = self._semantic_prepare(project, query)
        hybrid = semantic["vector"] is not None

        owns_snapshot = not self.db.in_transaction
        if owns_snapshot:
            self.db.execute("BEGIN")
        try:
            row = self.db.execute("SELECT generation,revocation_epoch,source_epoch FROM projects WHERE id=?", (project,)).fetchone()
            if row is None:
                raise ContractError("unknown project")
            manifest = dict(row)
            if hybrid and semantic["signature"] != self.semantic_signature(project):
                # The provider request ran without a database lock. A concurrent
                # refresh can change the vector space before this snapshot starts.
                hybrid = False
                semantic["vector"] = None
                semantic["diagnostics"].update(semantic_status="index_changed", retryable=True,
                                               retry_after_seconds=5)
            # 自动检索先有界过召回，再筛来源；来源变化必须使已筛掉的候选重新参与。
            source_epoch = manifest.pop("source_epoch")
            cache_identity = [self.semantic_signature(project), "hybrid"] if hybrid else "local-fts"
            key = digest([project, plan, limit, filters, manifest, cache_identity, INDEX_VERSION,
                          bool(_automatic), source_epoch if _automatic else None,
                          _environment if _automatic else None])
            cached = self.db.execute("SELECT body FROM cache WHERE key=?", (key,)).fetchone()
            references = None
            if cached:
                try:
                    references = json.loads(cached[0])
                    if (not isinstance(references, list) or len(references) > limit or any(
                            not isinstance(ref, dict) or set(ref) != {"knowledge_id", "revision"}
                            or not isinstance(ref["knowledge_id"], str)
                            or not isinstance(ref["revision"], int) or isinstance(ref["revision"], bool)
                            for ref in references)):
                        references = None
                except (ValueError, TypeError):
                    references = None

            conditions = _filter_sql(filters)
            weights = {word: 2 if _CHINESE.fullmatch(word) and len(word) > 1 else 1 for word in selected}
            denominator = sum(weights.values())
            # 只在本次快照中复用分析结果；缓存校验、解释和语义门槛共用同一修订的词元。
            analyses = {}
            unreadable = set()
            def analyze(row):
                identity = (row["id"], row["revision"])
                if identity not in analyses:
                    try:
                        analyses[identity] = _match_values(row["body"], weights, denominator)
                    except json.JSONDecodeError:
                        if not _automatic:
                            raise
                        unreadable.add(identity)
                        analyses[identity] = (0, 0, {})
                return analyses[identity]
            semantic_matches = self._semantic_candidates(project, semantic, filters) if hybrid else {}
            rows = []
            if references is not None:
                for ref in references:
                    row = self.db.execute("SELECT k.* FROM knowledge k JOIN active a ON k.project=a.project "
                                          "AND k.id=a.id AND k.revision=a.revision WHERE k.project=? "
                                          "AND k.id=? AND k.revision=?" + conditions,
                                          (project, ref["knowledge_id"], ref["revision"], *filters.values())).fetchone()
                    if row is None or (analyze(row)[0] <= 0
                                      and row["id"] not in semantic_matches):
                        references = None
                        rows = []
                        break
                    rows.append(row)
            recall = {"pages": 0, "examined": 0, "scan_limit_reached": False}
            page_state = {}
            from_cache = references is not None
            def lexical_order(row):
                values = analyze(row)
                exact = all(anchor in values[2] for anchor in plan["exact_anchors"])
                return (-int(exact), -values[0], row["id"])
            if references is None:
                rows = lexical_rows(self.db, project, recall_plan, filters, diagnostics=page_state)
                recall["pages"] = 1
                rows.sort(key=lexical_order)
                if hybrid:
                    lexical_ranks = {row["id"]: rank for rank, row in enumerate(rows, 1)}
                    merged = {row["id"]: row for row in rows}
                    merged.update({identity: match["row"] for identity, match in semantic_matches.items()})
                    def fusion(identity):
                        return (1 / (60 + lexical_ranks[identity]) if identity in lexical_ranks else 0) + (
                            1 / (60 + semantic_matches[identity]["rank"]) if identity in semantic_matches else 0)
                    rows = sorted(merged.values(), key=lambda row: (-fusion(row["id"]), row["id"]))
            # 正文及来源都在同一快照读取，禁止通过 read() 跳转到刚发布的新修订。
            items = []
            retained = []
            seen = set()
            # Refill only when rejected records would otherwise crowd out useful
            # knowledge. Each query stays bounded to four FTS pages; no health
            # protection or authority row is removed to make retrieval succeed.
            while rows:
                row = rows.pop(0)
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                recall["examined"] += 1
                values = analyze(row)
                identity = (row["id"], row["revision"])
                explanation = (_match_info(row["body"], weights, denominator, meaningful, _values=values,
                                           _anchors=plan["exact_anchors"]) if identity not in unreadable else
                               {"score": 0, "coverage": 0, "matched_terms": [], "auto_eligible": False})
                # 分词已统一；仍过滤派生投影异常或缓存中没有任何有效词重合的引用。
                semantic_match = semantic_matches.get(row["id"])
                if explanation["score"] <= 0 and semantic_match is None:
                    explanation["auto_eligible"] = False
                if hybrid:
                    # Cached references preserve ranking; scores remain query-specific.
                    explanation.update(method="hybrid", semantic_score=round(semantic_match["score"], 6)
                        if semantic_match else None, semantic_rank=semantic_match["rank"] if semantic_match else None,
                        fusion_method="reciprocal_rank_k60", semantic_input_truncated=bool(
                            semantic_match and semantic_match["input_truncated"]))
                    if semantic_match and not explanation["auto_eligible"]:
                        explanation["auto_eligible"] = bool(
                            semantic_match["score"] >= semantic["config"]["automatic_min_similarity"]
                            and any(len(token) > 1 for token in selected))
                # All channels, including a disabled or degraded semantic provider,
                # use the same complete identifier gate. Public matched_terms is
                # bounded for display and must never decide this check.
                if (identity not in unreadable and explanation["auto_eligible"]
                        and not _positive_anchors_allowed(row["body"], plan["exact_anchors"])):
                    explanation["auto_eligible"] = False
                if not plan["automatic_safe"] or identity in unreadable:
                    explanation["auto_eligible"] = False
                if (explanation["score"] > 0 or semantic_match is not None) and (
                        not _automatic or explanation["auto_eligible"]):
                    try:
                        item = self._public(project, row)
                    except json.JSONDecodeError:
                        if not _automatic:
                            raise
                        # An unreadable record is kept for explicit inspection;
                        # it must not abort optional automatic memory delivery.
                        unreadable.add(identity)
                        item = None
                        explanation["auto_eligible"] = False
                    if item is not None and _automatic and _environment:
                        from .knowledge_applicability import automatic_applicability
                        if automatic_applicability(item, _environment)["status"] == "incompatible":
                            explanation["auto_eligible"] = False
                    if item is not None and (item.get("source_health") in {"conflicted", "denied", "missing"}
                            or item.get("pending_counterexample_count", 0)):
                        explanation["auto_eligible"] = False
                    if item is not None and (not _automatic or explanation["auto_eligible"]):
                        item["match"] = explanation
                        items.append(item)
                        retained.append({"knowledge_id": row["id"], "revision": row["revision"]})
                if len(items) == limit:
                    break
                while not rows and _automatic and not from_cache and page_state.get("page_may_have_more"):
                    if recall["pages"] >= _MAX_RECALL_PAGES:
                        recall["scan_limit_reached"] = True
                        break
                    else:
                        page_state = {}
                        next_page = lexical_rows(self.db, project, recall_plan, filters, page=recall["pages"],
                                                 diagnostics=page_state)
                        recall["pages"] += 1
                        rows = sorted((item for item in next_page if item["id"] not in seen), key=lexical_order)
            references = retained
            if owns_snapshot:
                self.db.execute("COMMIT")
        except BaseException:
            if owns_snapshot and self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

        # 先结束只读快照再写派生缓存/计数，避免 WAL 读事务升级引发 BUSY_SNAPSHOT。
        def write_derived():
            # Retryable provider failure must not cache a hybrid absence and suppress
            # recovery. Existing lexical cache may still be read safely.
            if not semantic["diagnostics"].get("retryable") and not recall["scan_limit_reached"] and not unreadable:
                self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (key, encode(references)))
                self.db.execute("DELETE FROM cache WHERE rowid NOT IN "
                                "(SELECT rowid FROM cache ORDER BY rowid DESC LIMIT 256)")
            self._metric(project, "search_queries")
            self._metric(project, "search_matches", int(bool(items)))

        if owns_snapshot:
            try:
                with self.transaction():
                    write_derived()
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                # Python 3.10 的异常没有 sqlite_errorcode，仍兼容声明的最低运行版本。
                busy = (code & 255 in {5, 6}) if code is not None else str(exc).lower().startswith(
                    ("database is locked", "database table is locked"))
                if not busy:
                    raise
                # 结果已经按完整快照获得，派生缓存/计数锁竞争不能阻断检索。
        else:
            write_derived()
        result = {"status": "matched" if items else "no_match", "items": items, **manifest}
        if (plan["query_diagnostics"]["input_truncated"] or plan["query_diagnostics"]["terms_truncated"]
                or not plan["automatic_safe"]):
            result["query_diagnostics"] = plan["query_diagnostics"]
        if unreadable:
            recall["unreadable_records"] = len(unreadable)
        if recall["pages"] > 1 or recall["scan_limit_reached"] or unreadable:
            result["recall_diagnostics"] = recall
        if semantic["config"]["enabled"]:
            result["retrieval"] = semantic["diagnostics"]
        return result
