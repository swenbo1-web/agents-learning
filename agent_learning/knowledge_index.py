"""可重建的 active 知识投影；正文、修订和来源仍由 SQLite 权威表保存。"""
from __future__ import annotations

import json
import re
import sqlite3
from .semantic_retrieval import (SemanticRetrievalMixin, semantic_automatic_allowed,
                                 semantic_exact_anchors_allowed)


INDEX_VERSION = 4
_WORDS = re.compile(r"c\+\+(?:[0-9]+)?|c#|[a-z0-9_]+(?:[./:-][a-z0-9_]+)*|[\u3400-\u9fff]+")
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
_MAX_AUTOMATIC_CANDIDATES = 64


def terms(text):
    """索引与评分共用词元；技术标识同时保留整体及其可独立检索的组件。"""
    result = []
    for word in _WORDS.findall(text.lower()):
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
                result.extend(component for component in re.split(r"[./:_-]+", word) if component)
    return list(dict.fromkeys(result))


tokenize = terms


def _query_terms(query):
    # 请求修饰语不参与相关度；先保留双字和标识，再限制总量，避免单字占满预算。
    meaningful = _ZH_REQUEST.sub(" ", query[-8000:])
    selected = [word for word in terms(meaningful) if word not in _EN_REQUEST_WORDS]
    if any(_CHINESE.fullmatch(word) and len(word) > 1 for word in selected):
        selected = [word for word in selected if not (_CHINESE.fullmatch(word) and len(word) == 1)]
    # 技术标识通常比自然语言连接片段明确，超预算时优先保留。
    technical = [word for word in selected if not _CHINESE.fullmatch(word)]
    chinese = [word for word in selected if _CHINESE.fullmatch(word)]
    return (technical + chinese)[:_MAX_QUERY_TERMS], meaningful


def normalized_query_plan(query):
    """缓存和检索共享同一有效输入；短语顺序也参与自动匹配判断。"""
    selected, meaningful = _query_terms(query)
    return {"terms": selected, "meaningful": meaningful}


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
            for token in terms(value):
                if token in query_weights:
                    matches[token] = max(weight, matches.get(token, 0))
    values = body.get("metadata", {}).values()
    for token in terms(" ".join(value for value in values if isinstance(value, str))):
        if token in query_weights:
            matches[token] = max(2, matches.get(token, 0))
    coverage = sum(query_weights[token] for token in matches) / denominator
    field_score = sum(query_weights[token] * weight for token, weight in matches.items()) / denominator
    # 查询最多128词且权重最多2，因此相差一个词的覆盖增益始终大于字段增益。
    return coverage * 2048 + field_score, coverage, matches


def _rank(body_json, query_weights, denominator):
    """只使用当前知识和查询，其他项目、历史条目和词频不影响排名。"""
    return _match_values(body_json, query_weights, denominator)[0]


def _match_info(body_json, query_weights, denominator, query, *, _values=None):
    # 独立于 _rank，缓存命中仍生成可解释字段，但不重排已经固定的修订引用。
    score, coverage, matches = (_match_values(body_json, query_weights, denominator)
                                if _values is None else _values)
    strong = [token for token in matches if len(token) > 1]
    eligible = bool(strong) and coverage >= 0.65
    if not eligible and len(strong) >= 3:
        # 长中文请求会降低全句覆盖；连续四字的问题短语仍可作为明确命中依据。
        for segment in re.findall(r"[\u3400-\u9fff]+", query):
            run = 0
            for index in range(len(segment) - 1):
                run = run + 1 if segment[index:index + 2] in matches else 0
                if run >= 3:
                    eligible = True
                    break
            if eligible:
                break
    return {"score": round(score, 6), "coverage": round(coverage, 6),
            "matched_terms": [token for token in query_weights if token in matches][:12],
            "auto_eligible": eligible}


def _filter_sql(filters):
    # 字段已经过 knowledge_filters 白名单校验；值始终绑定为参数。
    conditions = []
    for field in filters:
        if field == "knowledge_kind":
            conditions.append(" AND COALESCE(json_extract(k.body,'$.knowledge_kind'),'solution')=?")
        elif field == "category":
            conditions.append(" AND json_extract(k.body,'$.category')=?")
        else:
            conditions.append(" AND json_extract(k.body,'$.metadata." + field + "')=?")
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

    def search(self, project, query, limit=5, filters=None, *, _automatic=False):
        from .runtime import ContractError, digest, encode, knowledge_filters

        if not isinstance(query, str):
            raise ContractError("query must be a string")
        limit = max(1, min(int(limit), 10))
        filters = knowledge_filters(filters)
        plan = normalized_query_plan(query)
        selected, meaningful = plan["terms"], plan["meaningful"]
        if not selected:
            return {"status": "no_match", "items": []}

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
                          bool(_automatic), source_epoch if _automatic else None])
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
            def analyze(row):
                identity = (row["id"], row["revision"])
                if identity not in analyses:
                    analyses[identity] = _match_values(row["body"], weights, denominator)
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
            if references is None:
                self.db.create_function("alp_knowledge_score", 1, lambda body: _rank(body, weights, denominator))
                match = " OR ".join('"' + word.replace('"', '""') + '"' for word in selected)
                rows = self.db.execute("SELECT k.* FROM knowledge_fts JOIN active a ON "
                                       "knowledge_fts.project=a.project AND knowledge_fts.id=a.id AND "
                                       "CAST(knowledge_fts.revision AS INTEGER)=a.revision JOIN knowledge k ON "
                                       "k.project=a.project AND k.id=a.id AND k.revision=a.revision "
                                       "WHERE knowledge_fts MATCH ? AND a.project=?" + conditions +
                                       " ORDER BY alp_knowledge_score(k.body) DESC,k.id ASC LIMIT ?",
                                       (match, project, *filters.values(),
                                        _MAX_AUTOMATIC_CANDIDATES if _automatic or hybrid else limit)).fetchall()
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
            for row in rows:
                values = analyze(row)
                explanation = _match_info(row["body"], weights, denominator, meaningful, _values=values)
                # 分词已统一；仍过滤派生投影异常或缓存中没有任何有效词重合的引用。
                semantic_match = semantic_matches.get(row["id"])
                if explanation["score"] <= 0 and semantic_match is None:
                    continue
                if hybrid:
                    full_matches = values[2]
                    # Cached references preserve ranking; scores remain query-specific.
                    explanation.update(method="hybrid", semantic_score=round(semantic_match["score"], 6)
                        if semantic_match else None, semantic_rank=semantic_match["rank"] if semantic_match else None,
                        fusion_method="reciprocal_rank_k60", semantic_input_truncated=bool(
                            semantic_match and semantic_match["input_truncated"]))
                    if semantic_match and not explanation["auto_eligible"]:
                        explanation["auto_eligible"] = semantic_automatic_allowed(
                            meaningful, full_matches, semantic_match, semantic["config"])
                if semantic["config"]["enabled"]:
                    # Optional-provider failure must not weaken the configured exact
                    # identifier gate when results fall back to the lexical channel.
                    full_matches = values[2]
                    if not semantic_exact_anchors_allowed(meaningful, full_matches):
                        explanation["auto_eligible"] = False
                item = self._public(project, row)
                if item.get("source_health") in {"conflicted", "denied", "missing"}:
                    explanation["auto_eligible"] = False
                if _automatic and not explanation["auto_eligible"]:
                    continue
                item["match"] = explanation
                items.append(item)
                retained.append({"knowledge_id": row["id"], "revision": row["revision"]})
                if len(items) == limit:
                    break
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
            if not semantic["diagnostics"].get("retryable"):
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
        if semantic["config"]["enabled"]:
            result["retrieval"] = semantic["diagnostics"]
        return result
