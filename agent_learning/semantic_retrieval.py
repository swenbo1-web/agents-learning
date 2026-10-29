"""Optional local embeddings and rebuildable vectors; SQLite knowledge remains authoritative.

Document embeddings are built only by explicit refresh. Queries never download models,
send data outside loopback, or perform network I/O while the caller holds a transaction.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
from http.client import HTTPConnection, HTTPException
import ipaddress
import json
import math
import operator
import re
import socket
import struct
import time
from urllib import error, parse, request

SEMANTIC_DEFAULTS = {
    "enabled": False, "provider": "ollama", "url": "http://127.0.0.1:11434", "model": "",
    "timeout_seconds": 2, "query_max_chars": 2000, "document_max_chars": 8000,
    "candidate_limit": 32, "scan_limit": 2000, "sync_batch_size": 16,
    "min_similarity": 0.55, "automatic_min_similarity": 0.85,
}
SEMANTIC_INDEX_VERSION = 1
# Selection changes invalidate query/context caches without rebuilding vectors.
SEMANTIC_SCAN_POLICY_VERSION = 3
_MAX_DIMENSIONS = 4096
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_FAILURE_COOLDOWN_SECONDS = 5


def validate_semantic_config(value):
    if not isinstance(value, dict) or set(value) - set(SEMANTIC_DEFAULTS):
        raise ValueError("retrieval.semantic must contain supported configuration fields")
    config = {**SEMANTIC_DEFAULTS, **value}
    if not isinstance(config["enabled"], bool) or config["provider"] != "ollama":
        raise ValueError("semantic provider must be ollama and enabled must be boolean")
    url = config["url"]
    if not isinstance(url, str) or len(url) > 500 or any(ord(char) < 32 for char in url):
        raise ValueError("semantic URL must be an HTTP loopback origin")
    host = ""
    try:
        parts = parse.urlsplit(url)
        host = parts.hostname or ""
        local = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        port = parts.port
    except ValueError:
        if host.lower() != "localhost":
            raise ValueError("semantic URL must be an HTTP loopback origin") from None
        local = True
        try:
            port = parts.port
        except ValueError:
            raise ValueError("invalid semantic URL port") from None
    if (parts.scheme != "http" or not local or parts.username is not None or parts.password is not None
            or parts.path not in {"", "/"} or parts.query or parts.fragment
            or port is not None and not 1 <= port <= 65535):
        raise ValueError("semantic URL must be an HTTP loopback origin without credentials or paths")
    config["url"] = url.rstrip("/")
    model = config["model"]
    if (not isinstance(model, str) or len(model) > 200 or model != model.strip()
            or any(ord(char) < 32 for char in model) or config["enabled"] and not model):
        raise ValueError("enabled semantic retrieval requires a nonempty model name")
    # Ollama also supports cloud models. This plugin deliberately offers a local provider.
    if re.search(r"(?i)(?:[:\-]cloud)(?:$|[:\-])", model):
        raise ValueError("semantic retrieval requires a local Ollama model")
    integers = {"query_max_chars": (128, 8000), "document_max_chars": (256, 32000),
                "candidate_limit": (1, 64), "scan_limit": (1, 10000), "sync_batch_size": (1, 32)}
    for field, (lower, upper) in integers.items():
        if (not isinstance(config[field], int) or isinstance(config[field], bool)
                or not lower <= config[field] <= upper):
            raise ValueError(f"invalid semantic {field}")
    for field, lower, upper in (("timeout_seconds", 0.1, 10), ("min_similarity", 0, 1),
                               ("automatic_min_similarity", 0, 1)):
        val = config[field]
        if (not isinstance(val, (int, float)) or isinstance(val, bool) or not math.isfinite(val)
                or not lower <= val <= upper):
            raise ValueError(f"invalid semantic {field}")
    if config["automatic_min_similarity"] < config["min_similarity"]:
        raise ValueError("automatic semantic threshold must be at least min_similarity")
    return config


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _vector_identity(config):
    return _hash([config["provider"], config["url"], config["model"],
                  config["document_max_chars"], SEMANTIC_INDEX_VERSION])


def _document(body_json, max_chars):
    from .runtime import clean
    body = clean(json.loads(body_json))
    fields = [body.get(field, "") for field in ("title", "content", "applicability", "limitations",
                                               "category", "knowledge_kind")]
    fields += [key + ": " + value for key, value in sorted(body.get("metadata", {}).items())
               if isinstance(value, str)]
    text = "\n".join(value for value in fields if isinstance(value, str))[:max_chars]
    # Bind to all authoritative fields, including the tail beyond the provider input budget.
    return text, _hash(body), len("\n".join(value for value in fields if isinstance(value, str))) > max_chars


def _normalize(values, expected=None):
    if (not isinstance(values, list) or not values or len(values) > _MAX_DIMENSIONS
            or expected is not None and len(values) != expected):
        raise ValueError("invalid_embedding_dimensions")
    try:
        valid = all(not isinstance(value, bool) and isinstance(value, (int, float))
                    and math.isfinite(value) for value in values)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid_embedding_values")
    # Scaling avoids overflow for finite but unreasonably large numeric values.
    scale = max(abs(value) for value in values)
    if scale == 0:
        raise ValueError("empty_embedding")
    scaled = [value / scale for value in values]
    norm = math.sqrt(sum(value * value for value in scaled))
    return [value / norm for value in scaled]


def _stored_vector(blob, dimensions, expected=None):
    if (not isinstance(dimensions, int) or isinstance(dimensions, bool) or not 1 <= dimensions <= _MAX_DIMENSIONS
            or expected is not None and dimensions != expected or not isinstance(blob, bytes)
            or len(blob) != dimensions * 4):
        raise ValueError("invalid_stored_vector")
    vector = struct.unpack("<" + "f" * dimensions, blob)
    norm_sq = sum(value * value for value in vector)
    if not math.isfinite(norm_sq) or abs(norm_sq - 1) > 0.0001:
        raise ValueError("invalid_stored_vector")
    return vector


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("embedding_redirect_rejected")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("embedding deadline exceeded")
    return remaining


class _DeadlineSocket(socket.socket):
    """SocketIO must renew the remaining total budget, not the original timeout."""
    def __init__(self, *args, deadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline

    def connect(self, address):
        self.settimeout(_remaining(self.deadline))
        return super().connect(address)

    def sendall(self, data, flags=0):
        self.settimeout(_remaining(self.deadline))
        return super().sendall(data, flags)

    def recv_into(self, buffer, nbytes=0, flags=0):
        self.settimeout(_remaining(self.deadline))
        return super().recv_into(buffer, nbytes, flags)


class _DeadlineHTTPConnection(HTTPConnection):
    def __init__(self, *args, deadline, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline
        self._create_connection = self._connect_socket

    def _connect_socket(self, address, timeout, source_address):
        host, port = address
        # Configuration only permits loopback. Avoid even a local resolver stall;
        # try the two literal localhost addresses within the same total budget.
        hosts = ("127.0.0.1", "::1") if host.lower() == "localhost" else (host,)
        last_error = None
        for host in hosts:
            for family, kind, protocol, _, target in socket.getaddrinfo(
                    host, port, 0, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST):
                sock = None
                try:
                    sock = _DeadlineSocket(family, kind, protocol, deadline=self.deadline)
                    if source_address:
                        sock.bind(source_address)
                    sock.connect(target)
                    return sock
                except OSError as exc:
                    last_error = exc
                    if sock is not None:
                        sock.close()
                    _remaining(self.deadline)
        if last_error is not None:
            raise last_error
        raise OSError("no loopback address available")


class _DeadlineHTTPHandler(request.HTTPHandler):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def http_open(self, req):
        def connection(host, **kwargs):
            return _DeadlineHTTPConnection(host, deadline=self.deadline, **kwargs)
        return self.do_open(connection, req)


def embed_texts(config, texts, expected_dimension=None):
    """One bounded request to an explicitly configured, already-running local model."""
    if not texts or len(texts) > config["sync_batch_size"]:
        raise ValueError("invalid_embedding_batch")
    payload = json.dumps({"model": config["model"], "input": texts, "truncate": False},
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    deadline = time.monotonic() + config["timeout_seconds"]
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(), _DeadlineHTTPHandler(deadline))
    req = request.Request(config["url"] + "/api/embed", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener.open(req, timeout=_remaining(deadline)) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        _remaining(deadline)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("embedding_response_too_large")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError("invalid_embedding_response") from None
        vectors = body.get("embeddings") if isinstance(body, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("invalid_embedding_response")
        normalized = []
        for vector in vectors:
            normalized.append(_normalize(vector, expected_dimension))
            expected_dimension = len(normalized[0])
        return normalized
    except (HTTPException, error.URLError, OSError, UnicodeError, json.JSONDecodeError):
        # Do not persist server bodies, paths, headers, credentials, or provider exception text.
        raise ValueError("embedding_provider_unavailable") from None


class SemanticRetrievalMixin:
    def _semantic_config(self):
        return validate_semantic_config(self.config.get("retrieval", {}).get("semantic", {}))

    def _semantic_state(self, project, config):
        exists = self.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN "
            "('semantic_index_state','semantic_vectors') AND type='table'").fetchone()[0] == 2
        return self.db.execute("SELECT * FROM semantic_index_state WHERE project=? AND identity=?",
                               (project, _vector_identity(config))).fetchone() if exists else None

    def semantic_signature(self, project):
        config = self._semantic_config()
        if not config["enabled"]:
            return "disabled"
        state = self._semantic_state(project, config)
        return _hash([config, state["generation"] if state else 0,
                      state["dimensions"] if state else 0, SEMANTIC_INDEX_VERSION,
                      SEMANTIC_SCAN_POLICY_VERSION])

    def semantic_status(self, project):
        from .runtime import ContractError
        if not self.db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
            raise ContractError("unknown project")
        config = self._semantic_config()
        active = self.db.execute("SELECT COUNT(*) FROM active WHERE project=?", (project,)).fetchone()[0]
        result = {"status": "disabled" if not config["enabled"] else "not_built", "enabled": config["enabled"],
                  "provider": config["provider"], "model": config["model"], "active": active,
                  "indexed": 0, "pending": active, "index_version": SEMANTIC_INDEX_VERSION}
        state = self._semantic_state(project, config)
        if state:
            indexed = 0
            rows = self.db.execute("SELECT v.content_hash,v.dimensions,v.vector,k.body FROM semantic_vectors v JOIN active a ON "
                "a.project=v.project AND a.id=v.knowledge_id AND a.revision=v.revision JOIN knowledge k ON "
                "k.project=a.project AND k.id=a.id AND k.revision=a.revision WHERE v.project=? AND v.identity=?",
                (project, _vector_identity(config)))
            for row in rows:
                try:
                    _stored_vector(row["vector"], row["dimensions"], state["dimensions"])
                except ValueError:
                    continue
                indexed += int(row["content_hash"] == _document(row["body"], config["document_max_chars"])[1])
            result.update(status="disabled" if not config["enabled"] else "ready" if indexed == active else "partial", indexed=indexed,
                          pending=active - indexed, dimensions=state["dimensions"], generation=state["generation"])
        return result

    def semantic_refresh(self, project, limit=100, rebuild=False):
        from .runtime import ContractError
        if (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000
                or not isinstance(rebuild, bool)):
            raise ContractError("semantic refresh requires limit in 1..1000 and boolean rebuild")
        if self.db.in_transaction:
            raise ContractError("semantic refresh must run outside a transaction")
        status = self.semantic_status(project)
        config = self._semantic_config()
        if not config["enabled"]:
            return {**status, "processed": 0, "requests": 0}
        identity = _vector_identity(config)
        with self.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS semantic_index_state(project TEXT NOT NULL,identity TEXT NOT NULL,"
                "generation INTEGER NOT NULL DEFAULT 0,dimensions INTEGER NOT NULL DEFAULT 0,"
                "PRIMARY KEY(project,identity))")
            self.db.execute("CREATE TABLE IF NOT EXISTS semantic_vectors(project TEXT NOT NULL,knowledge_id TEXT NOT NULL,"
                "revision INTEGER NOT NULL,identity TEXT NOT NULL,content_hash TEXT NOT NULL,dimensions INTEGER NOT NULL,"
                "vector BLOB NOT NULL,input_truncated INTEGER NOT NULL,PRIMARY KEY(project,knowledge_id,identity))")
            self.db.execute("INSERT OR IGNORE INTO semantic_index_state(project,identity) VALUES(?,?)", (project, identity))
            removed = self.db.execute("DELETE FROM semantic_vectors WHERE project=? AND (identity<>? OR NOT EXISTS "
                "(SELECT 1 FROM active a WHERE a.project=semantic_vectors.project AND a.id=semantic_vectors.knowledge_id "
                "AND a.revision=semantic_vectors.revision))", (project, identity)).rowcount
            if removed:
                self.db.execute("UPDATE semantic_index_state SET generation=generation+1 WHERE project=? AND identity=?",
                                (project, identity))
        state = self._semantic_state(project, config)
        dimension = None if rebuild else state["dimensions"] or None
        expected_generation = state["generation"]
        # A bounded batch is materialized before the provider request; refresh is incremental.
        rows = self.db.execute("SELECT k.*,v.content_hash,v.dimensions AS vector_dimensions,v.vector FROM knowledge k JOIN active a ON "
            "a.project=k.project AND a.id=k.id AND a.revision=k.revision LEFT JOIN semantic_vectors v ON "
            "v.project=k.project AND v.knowledge_id=k.id AND v.revision=k.revision AND v.identity=? "
            "WHERE k.project=? ORDER BY k.id", (identity, project))
        pending = []
        for row in rows:
            text, content_hash, truncated = _document(row["body"], config["document_max_chars"])
            try:
                _stored_vector(row["vector"], row["vector_dimensions"], dimension)
                valid_vector = dimension is not None
            except ValueError:
                valid_vector = False
            if rebuild or not valid_vector or content_hash != row["content_hash"]:
                pending.append((row["id"], row["revision"], text, content_hash, truncated))
                if len(pending) == limit:
                    break
        processed, requests, skipped = 0, 0, 0
        clear_old = rebuild
        for offset in range(0, len(pending), config["sync_batch_size"]):
            batch = pending[offset:offset + config["sync_batch_size"]]
            requests += 1
            try:
                vectors = embed_texts(config, [row[2] for row in batch], dimension)
            except ValueError as exc:
                return {**self.semantic_status(project), "status": "degraded", "reason": str(exc),
                        "processed": processed, "requests": requests, "skipped": skipped, "retryable": True}
            dimension = len(vectors[0])
            with self.transaction():
                current_state = self._semantic_state(project, config)
                if current_state["generation"] != expected_generation:
                    return {**self.semantic_status(project), "status": "degraded", "reason": "semantic_index_changed",
                            "processed": processed, "requests": requests, "skipped": skipped, "retryable": True}
                if clear_old:
                    self.db.execute("DELETE FROM semantic_vectors WHERE project=? AND identity=?", (project, identity))
                    clear_old = False
                for row, vector in zip(batch, vectors):
                    current = self.db.execute("SELECT k.body FROM knowledge k JOIN active a ON "
                        "a.project=k.project AND a.id=k.id AND a.revision=k.revision WHERE k.project=? AND k.id=? "
                        "AND k.revision=?", (project, row[0], row[1])).fetchone()
                    if current is None or _document(current[0], config["document_max_chars"])[1] != row[3]:
                        skipped += 1
                        continue
                    self.db.execute("INSERT OR REPLACE INTO semantic_vectors VALUES(?,?,?,?,?,?,?,?)",
                        (project, row[0], row[1], identity, row[3], dimension,
                         struct.pack("<" + "f" * dimension, *vector), int(row[4])))
                    processed += 1
                self.db.execute("UPDATE semantic_index_state SET dimensions=?,generation=generation+1 "
                                "WHERE project=? AND identity=?", (dimension, project, identity))
                expected_generation += 1
        if clear_old and not pending:
            with self.transaction():
                self.db.execute("DELETE FROM semantic_vectors WHERE project=? AND identity=?", (project, identity))
                self.db.execute("UPDATE semantic_index_state SET dimensions=0,generation=generation+1 "
                                "WHERE project=? AND identity=?", (project, identity))
        return {**self.semantic_status(project), "processed": processed, "requests": requests, "skipped": skipped}

    def _semantic_prepare(self, project, query):
        from .runtime import clean, clip, ContractError
        config = self._semantic_config()
        diagnostics = {"mode": "hybrid", "semantic_status": "disabled", "retryable": False}
        prepared = {"config": config, "diagnostics": diagnostics, "vector": None}
        if not config["enabled"]:
            return prepared
        if not self.db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
            raise ContractError("unknown project")
        state = self._semantic_state(project, config)
        if (not state or not 1 <= state["dimensions"] <= _MAX_DIMENSIONS or not self.db.execute(
                "SELECT 1 FROM semantic_vectors WHERE project=? AND identity=? LIMIT 1",
                (project, _vector_identity(config))).fetchone()):
            diagnostics.update(semantic_status="not_built", reason="run_semantic_refresh")
            return prepared
        prepared["signature"] = self.semantic_signature(project)
        # Bound both provider input and cache keys; use clean before leaving the process.
        text = clip(clean(query).strip(), config["query_max_chars"])
        if not text:
            diagnostics.update(semantic_status="empty_query")
            return prepared
        key = _hash([_vector_identity(config), state["dimensions"], state["generation"], text])
        # Internal selection inputs only: diagnostics never disclose the query.
        prepared.update(query_text=text, query_key=key,
                        scan_key=_hash([_vector_identity(config), text])[:32])
        cache = getattr(self, "_semantic_query_vectors", None)
        if cache is None:
            self._semantic_query_vectors = cache = OrderedDict()
        vector = cache.get(key)
        if vector is not None:
            cache.move_to_end(key)
            prepared["vector"] = vector
            diagnostics.update(semantic_status="ready", query_cache_hit=True, query_truncated=len(query) > len(text))
            return prepared
        failures = getattr(self, "_semantic_query_failures", None)
        if failures is None:
            self._semantic_query_failures = failures = OrderedDict()
        failed = failures.get(key)
        if failed and failed[0] > time.monotonic():
            diagnostics.update(semantic_status="degraded", reason=failed[1], retryable=True,
                               retry_after_seconds=round(failed[0] - time.monotonic(), 2))
            return prepared
        if self.db.in_transaction:
            diagnostics.update(semantic_status="transaction_deferred", retryable=True)
            return prepared
        try:
            prepared["vector"] = embed_texts(config, [text], state["dimensions"])[0]
        except ValueError as exc:
            diagnostics.update(semantic_status="degraded", reason=str(exc), retryable=True,
                               retry_after_seconds=_FAILURE_COOLDOWN_SECONDS)
            failures[key] = (time.monotonic() + _FAILURE_COOLDOWN_SECONDS, str(exc))
            while len(failures) > 128:
                failures.popitem(last=False)
            return prepared
        failures.pop(key, None)
        cache[key] = prepared["vector"]
        while len(cache) > 128:
            cache.popitem(last=False)
        diagnostics.update(semantic_status="ready", query_cache_hit=False, query_truncated=len(query) > len(text))
        return prepared

    def _semantic_scan_rows(self, project, prepared, filters):
        """Select a stable, budget-monotone prefix of three candidate streams.

        The historical cursor depends on the query, never population size or
        index generation. ID range scans avoid COUNT/OFFSET; the recent stream
        can use the knowledge_recent index. Filters and sparse active vectors
        may still require examining more metadata than returned identities.
        This bounded selection is not ANN or a complete-nearest-neighbor claim.
        """
        from .knowledge_index import _filter_sql, lexical_rows, normalized_query_plan
        config = prepared["config"]
        limit = config["scan_limit"]
        identity = _vector_identity(config)
        # Keep knowledge as the driving range/order cursor. A planner that
        # starts from every vector defeats both the ID seek and recent index.
        source = (" FROM knowledge k CROSS JOIN active a ON a.project=k.project AND a.id=k.id "
            "AND a.revision=k.revision CROSS JOIN semantic_vectors v ON v.project=k.project "
            "AND v.knowledge_id=k.id AND v.revision=k.revision "
            "WHERE k.project=? AND v.identity=?" + _filter_sql(filters))
        parameters = (project, identity, *filters.values())
        columns = "SELECT k.*,v.content_hash,v.dimensions,v.vector,v.input_truncated"
        # The probe reads identities, not every authoritative body/vector.
        probe = self.db.execute("SELECT k.id" + source + " ORDER BY k.id LIMIT ?",
                                (*parameters, limit + 1)).fetchall()
        if len(probe) <= limit:
            prepared["diagnostics"].update(scan_policy="complete", eligible_vectors=len(probe),
                scan_truncated=False, scan_lanes={"complete": len(probe)})
            return self.db.execute(columns + source + " ORDER BY k.id LIMIT ?", (*parameters, limit)).fetchall()

        scan_key = prepared.get("scan_key") or _hash(
            [identity, prepared.get("query_text") or prepared["vector"]])[:32]
        streams = {"lexical": [], "recent": [], "historical": []}
        query = prepared.get("query_text", "")
        if query:
            plan = normalized_query_plan(query)
            if plan["terms"]:
                # A fixed seed prefix preserves lane ordering when scan_limit
                # changes, including lexical_rows' multiple recall lanes.
                lexical = lexical_rows(self.db, project, plan, filters, max_candidates=128)
                if lexical:
                    placeholders = ",".join("?" for _ in lexical)
                    eligible = {row[0] for row in self.db.execute("SELECT k.id" + source +
                        " AND k.id IN (" + placeholders + ")", (*parameters, *(row["id"] for row in lexical)))}
                    for row in lexical:
                        if row["id"] in eligible:
                            streams["lexical"].append(row["id"])

        streams["recent"] = [row[0] for row in self.db.execute(
            "SELECT k.id" + source + " ORDER BY k.created DESC,k.id LIMIT ?", (*parameters, limit))]
        history = self.db.execute("SELECT k.id" + source + " AND k.id>=? ORDER BY k.id LIMIT ?",
                                 (*parameters, scan_key, limit)).fetchall()
        if len(history) < limit:
            history += self.db.execute("SELECT k.id" + source + " AND k.id<? ORDER BY k.id LIMIT ?",
                                       (*parameters, scan_key, limit - len(history))).fetchall()
        streams["historical"] = [row[0] for row in history]
        # Each variable-sized stream has at least `limit` unique IDs when the
        # collection is truncated. Its fetched prefix therefore cannot run out
        # before `limit` distinct selections. Fixed interleaving makes a larger
        # budget extend the existing set rather than rebalance away old hits.
        cursors = {name: iter(values) for name, values in streams.items()}
        selected = {}
        lanes = {name: 0 for name in streams}
        cycle = ("historical", "lexical", "recent", "historical")
        while len(selected) < limit:
            before = len(selected)
            for name in cycle:
                for identifier in cursors[name]:
                    if identifier not in selected:
                        selected[identifier] = None
                        lanes[name] += 1
                        break
                if len(selected) == limit:
                    break
            if len(selected) == before:
                break

        # Chunk parameters for SQLite builds with a 999-variable limit.
        identities = list(selected)
        rows = []
        for start in range(0, len(identities), 256):
            batch = identities[start:start + 256]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(self.db.execute(columns + source + " AND k.id IN (" + placeholders + ")",
                                       (*parameters, *batch)).fetchall())
        prepared["diagnostics"].update(scan_policy="stable_interleaved_keyset",
            eligible_vectors=None, eligible_vectors_lower_bound=len(probe),
            scan_truncated=True, scan_lanes=lanes, metadata_selection="indexed_ranges")
        return rows

    def _semantic_candidates(self, project, prepared, filters):
        config, vector = prepared["config"], prepared["vector"]
        if vector is None:
            return {}
        rows = self._semantic_scan_rows(project, prepared, filters)
        matches, invalid = [], 0
        for row in rows:
            try:
                content_hash = _document(row["body"], config["document_max_chars"])[1]
            except json.JSONDecodeError:
                # One unreadable authoritative body invalidates its optional vector,
                # not the other healthy candidates. SQL/identity errors still escape.
                invalid += 1
                continue
            if row["content_hash"] != content_hash:
                invalid += 1
                continue
            try:
                values = _stored_vector(row["vector"], row["dimensions"], len(vector))
            except ValueError:
                invalid += 1
                continue
            similarity = max(-1.0, min(1.0, sum(map(operator.mul, vector, values))))
            if similarity >= config["min_similarity"]:
                matches.append((similarity, row["id"], row))
                if len(matches) > config["candidate_limit"]:
                    matches.sort(key=lambda item: (-item[0], item[1]))
                    del matches[config["candidate_limit"]:]
        matches.sort(key=lambda item: (-item[0], item[1]))
        prepared["diagnostics"].update(scanned=len(rows), invalid_vectors=invalid)
        return {row["id"]: {"row": row, "score": score, "rank": rank,
                             "input_truncated": bool(row["input_truncated"])}
                for rank, (score, _, row) in enumerate(matches[:config["candidate_limit"]], 1)}


def semantic_exact_anchors_allowed(query, matched_terms):
    """Full lexical matches, not the bounded public explanation, enforce exact anchors."""
    from .knowledge_index import _query_terms
    tokens = _query_terms(query)[0]
    anchors = [token for token in tokens if re.search(r"\d", token) or any(char in token for char in ".:/")
               or token in {"cplusplus", "csharp"}]
    return all(anchor in matched_terms for anchor in anchors)


def semantic_automatic_allowed(query, matched_terms, semantic, config):
    if semantic["score"] < config["automatic_min_similarity"]:
        return False
    # A natural-language embedding cannot override a different exact error code, path,
    # version, or language identifier. Explicit filters are also applied in SQL.
    from .knowledge_index import _query_terms
    tokens = _query_terms(query)[0]
    if not any(len(token) > 1 for token in tokens):
        return False
    return semantic_exact_anchors_allowed(query, matched_terms)
