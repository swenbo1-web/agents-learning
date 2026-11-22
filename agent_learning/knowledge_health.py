"""Bounded, disposable knowledge diagnostics shared by all host installations.

These observations never change knowledge, enqueue reviews or prove truth.
The installer does not open a database; the first bound project advances it.
"""
from __future__ import annotations

from collections import Counter
import json
import sqlite3
import time

RULE_VERSION = 2
MAX_BODY = 65536
MAX_SOURCES = 8
MAX_REFS = 64


def install(runtime):
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_health_runs(
        project TEXT NOT NULL,rule_version INTEGER NOT NULL,state TEXT NOT NULL,
        cursor TEXT NOT NULL,ceiling TEXT NOT NULL,scanned INTEGER NOT NULL,
        affected INTEGER NOT NULL,issue_counts TEXT NOT NULL,generation INTEGER NOT NULL,
        revocation_epoch INTEGER NOT NULL,started REAL NOT NULL,updated REAL NOT NULL,completed REAL,
        PRIMARY KEY(project,rule_version),FOREIGN KEY(project) REFERENCES projects(id))""")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_health_items(
        project TEXT NOT NULL,rule_version INTEGER NOT NULL,knowledge_id TEXT NOT NULL,
        revision INTEGER NOT NULL,title TEXT NOT NULL,issues TEXT NOT NULL,checked REAL NOT NULL,
        PRIMARY KEY(project,rule_version,knowledge_id),
        FOREIGN KEY(project,rule_version) REFERENCES knowledge_health_runs(project,rule_version))""")
    runtime.db.execute("CREATE INDEX IF NOT EXISTS knowledge_health_findings "
                       "ON knowledge_health_items(project,rule_version,knowledge_id) WHERE issues!='[]'")
    runtime.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_health_pending(
        project TEXT NOT NULL,knowledge_id TEXT NOT NULL,PRIMARY KEY(project,knowledge_id),
        FOREIGN KEY(project) REFERENCES projects(id) ON DELETE CASCADE)""")
    _install_pending_triggers(runtime.db)


def _install_pending_triggers(db):
    def enqueue(name, event, targets, *, condition=""):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS knowledge_health_pending_{name} {event} {condition} BEGIN "
                   "INSERT INTO knowledge_health_pending(project,knowledge_id) "
                   f"{targets} ON CONFLICT(project,knowledge_id) DO NOTHING; END")

    # Removing an active duplicate can also clear the finding on its peer.
    def active_peers(identity):
        return (f"SELECT {identity}.project,{identity}.id UNION "
                "SELECT a.project,a.id FROM knowledge_fingerprints f JOIN knowledge_fingerprints peer "
                "ON peer.project=f.project AND peer.fingerprint=f.fingerprint AND peer.id!=f.id "
                "JOIN active a ON a.project=peer.project AND a.id=peer.id AND a.revision=peer.revision "
                f"WHERE f.project={identity}.project AND f.id={identity}.id AND f.revision={identity}.revision")

    enqueue("active_insert", "AFTER INSERT ON active", active_peers("NEW"))
    enqueue("active_delete", "AFTER DELETE ON active", active_peers("OLD"))
    enqueue("active_revision", "AFTER UPDATE OF revision ON active", active_peers("OLD") + " UNION " + active_peers("NEW"),
            condition="WHEN OLD.revision IS NOT NEW.revision")

    def active_target(identity):
        return (f"SELECT {identity}.project,{identity}.knowledge_id WHERE EXISTS("
                f"SELECT 1 FROM active a WHERE a.project={identity}.project AND a.id={identity}.knowledge_id "
                f"AND a.revision={identity}.revision)")

    # This projection already changes when cited evidence, source links or
    # problem revisions change. Do not add a second reverse evidence index.
    enqueue("source_insert", "AFTER INSERT ON knowledge_source_versions", active_target("NEW"))
    enqueue("source_version", "AFTER UPDATE OF version ON knowledge_source_versions", active_target("NEW"),
            condition="WHEN OLD.version IS NOT NEW.version")
    enqueue("feedback_insert", "AFTER INSERT ON knowledge_feedback", active_target("NEW"),
            condition="WHEN NEW.kind='counterexample'")
    enqueue("feedback_disposition", "AFTER UPDATE OF disposition ON knowledge_feedback", active_target("NEW"),
            condition="WHEN NEW.kind='counterexample' AND OLD.disposition IS NOT NEW.disposition")
    enqueue("feedback_delete", "AFTER DELETE ON knowledge_feedback", active_target("OLD"),
            condition="WHEN OLD.kind='counterexample'")


def _issues(runtime, project, row):
    from .runtime import CATEGORIES, KNOWLEDGE_KINDS, VALIDATION_LEVELS
    from .source_caveats import summarize_sources
    db, issues = runtime.db, []
    if row['body'] is None:
        return '', ['knowledge_record_missing']
    if len(row['body']) > MAX_BODY:
        return '', ['body_check_partial']
    try:
        body = json.loads(row['body'])
    except (ValueError, TypeError, RecursionError, OverflowError):
        return '', ['body_invalid']
    if not isinstance(body, dict):
        return '', ['body_invalid']
    if not isinstance(body.get('category'), str) or body['category'] not in CATEGORIES:
        issues.append('category_missing_or_invalid')
    elif body['category'] == 'unknown':
        issues.append('category_unknown')
    if not isinstance(body.get('knowledge_kind'), str) or body['knowledge_kind'] not in KNOWLEDGE_KINDS:
        issues.append('kind_missing_or_invalid')
    if not isinstance(body.get('validation_level'), str) or body['validation_level'] not in VALIDATION_LEVELS:
        issues.append('validation_missing_or_invalid')
    for field in ('applicability', 'limitations'):
        if not isinstance(body.get(field), str) or not body[field].strip():
            issues.append(field + '_unspecified')
    duplicate = db.execute(
        'SELECT 1 FROM knowledge_fingerprints f JOIN knowledge_fingerprints d '
        'ON d.project=f.project AND d.fingerprint=f.fingerprint AND d.id!=f.id '
        'JOIN active a ON a.project=d.project AND a.id=d.id AND a.revision=d.revision '
        'WHERE f.project=? AND f.id=? AND f.revision=? LIMIT 1',
        (project, row['id'], row['revision'])).fetchone()
    if duplicate:
        issues.append('exact_duplicate')
    if db.execute("SELECT 1 FROM knowledge_feedback WHERE project=? AND knowledge_id=? AND revision=? "
                  "AND kind='counterexample' AND disposition='unresolved' LIMIT 1",
                  (project, row['id'], row['revision'])).fetchone():
        issues.append('unresolved_counterexample')
    sources = db.execute(
        'SELECT candidate_id,problem_id,problem_revision,substr(evidence_refs,1,?) AS refs '
        'FROM knowledge_sources WHERE project=? AND knowledge_id=? AND revision=? '
        'ORDER BY candidate_id LIMIT ?', (MAX_BODY + 1, project, row['id'], row['revision'], MAX_SOURCES + 1)).fetchall()
    if not sources:
        # A legacy fallback is not evidence that provenance is intact.
        issues.append('sources_missing')
    checked_sources, remaining = [], MAX_REFS
    if len(sources) > MAX_SOURCES:
        issues.append('source_check_partial')
    for source in sources[:MAX_SOURCES]:
        problem = db.execute('SELECT revision FROM problems WHERE project=? AND id=?',
                             (project, source['problem_id'])).fetchone()
        if not problem:
            issues.append('source_missing')
        elif problem[0] != source['problem_revision']:
            issues.append('source_problem_changed')
        try:
            refs = json.loads(source['refs']) if len(source['refs']) <= MAX_BODY else None
        except (ValueError, TypeError, RecursionError, OverflowError):
            refs = None
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            issues.append('source_check_partial')
            continue
        if not refs:
            issues.append('source_missing')
        complete_source = len(refs) <= remaining
        if not complete_source:
            issues.append('source_check_partial')
        evidence = []
        for ref in dict.fromkeys(refs[:remaining]):
            remaining -= 1
            event = db.execute(
                'SELECT e.type,e.outcome,e.rowid AS sequence,o.conflict,o.decision '
                'FROM events e LEFT JOIN operations o ON o.project=e.project AND o.id=e.operation '
                'WHERE e.project=? AND e.id=? AND EXISTS('
                'SELECT 1 FROM problem_events p WHERE p.problem=? AND p.event=e.id)',
                (project, ref, source['problem_id'])).fetchone()
            if not event:
                issues.append('source_missing')
                complete_source = False
                continue
            health = ('conflicted' if event['conflict'] or event['outcome'] == 'conflict' else
                      'denied' if event['decision'] == 'denied' or event['outcome'] == 'denied' else 'valid')
            if health != 'valid':
                issues.append('source_' + health)
            evidence.append({'outcome': event['outcome'], 'sequence': event['sequence'], 'health': health,
                             'current_tool_success': health == 'valid' and event['type'] == 'PostToolUse'
                             and event['outcome'] == 'succeeded'})
        if complete_source:
            checked_sources.append({'candidate_id': source['candidate_id'], 'health': 'valid', 'evidence': evidence})
        elif any(event['outcome'] == 'failed' for event in evidence):
            issues.append('source_observed_failure_recovery_unknown')
    if summarize_sources(checked_sources)['unresolved_source_count']:
        issues.append('unresolved_source_failure')
    title = body.get('title', '')
    return title[:160] if isinstance(title, str) else '', sorted(set(issues))


def _has_pending(db, project):
    return db.execute('SELECT 1 FROM knowledge_health_pending WHERE project=? LIMIT 1', (project,)).fetchone() is not None


def _covers_project(run, manifest):
    return (run['state'] == 'completed' and manifest is not None
            and run['generation'] == manifest['generation'] and run['revocation_epoch'] == manifest['revocation_epoch'])


def _project_manifest(db, project):
    return db.execute('SELECT generation,revocation_epoch FROM projects WHERE id=?', (project,)).fetchone()


def advance(runtime, project, *, max_items=12, max_seconds=.075):
    """One short atomic page. No long lease, host key, model or persistent worker."""
    if type(max_items) is not int or not 1 <= max_items <= 200 or not 0 < max_seconds <= 2:
        raise ValueError('invalid knowledge health batch budget')
    db = runtime.db
    if db.in_transaction:
        return {'state': 'deferred', 'reason': 'outer_transaction'}
    # A single read avoids taking the writer lock when this checkpoint is
    # complete, including on ordinary project opens from the other hosts.
    latest = db.execute("SELECT r.rule_version,r.state,("
                        "r.state='completed' AND r.generation=p.generation AND r.revocation_epoch=p.revocation_epoch "
                        "AND NOT EXISTS(SELECT 1 FROM knowledge_health_pending d WHERE d.project=r.project)"
                        ") AS current FROM knowledge_health_runs r JOIN projects p ON p.id=r.project "
                        "WHERE r.project=? ORDER BY r.rule_version DESC LIMIT 1", (project,)).fetchone()
    if latest and (latest['rule_version'] > RULE_VERSION or latest['rule_version'] == RULE_VERSION and latest['current']):
        return {'state': latest['state'], 'advanced': 0, 'rule_version': latest['rule_version']}
    previous_timeout = db.execute('PRAGMA busy_timeout').fetchone()[0]
    deadline = time.monotonic() + max_seconds
    try:
        db.execute('PRAGMA busy_timeout=0')
        db.execute('BEGIN IMMEDIATE')
        # Recheck after acquiring the shared writer lock: another host may have finished.
        latest = db.execute('SELECT * FROM knowledge_health_runs WHERE project=? '
                            'ORDER BY rule_version DESC LIMIT 1', (project,)).fetchone()
        manifest = _project_manifest(db, project)
        if latest and (latest['rule_version'] > RULE_VERSION or
                       latest['rule_version'] == RULE_VERSION and _covers_project(latest, manifest)
                       and not _has_pending(db, project)):
            db.commit()
            return {'state': latest['state'], 'advanced': 0, 'rule_version': latest['rule_version']}
        if manifest is None:
            raise ValueError('unknown knowledge health project')
        if not latest or latest['rule_version'] < RULE_VERSION:
            last = db.execute('SELECT id FROM active WHERE project=? ORDER BY id DESC LIMIT 1', (project,)).fetchone()
            now = time.time()
            db.execute('INSERT INTO knowledge_health_runs VALUES(?,?,?, ?,?,0,0,?, ?,?,?,?,NULL)',
                       (project, RULE_VERSION, 'checking', '', last[0] if last else '', '{}',
                        manifest['generation'], manifest['revocation_epoch'], now, now))
            latest = db.execute('SELECT * FROM knowledge_health_runs WHERE project=? AND rule_version=?',
                                (project, RULE_VERSION)).fetchone()
        # The first bounded sweep covers pre-upgrade rows without needing a
        # full-table queue backfill. Later passes fetch only changed identities.
        cursor, ceiling = latest['cursor'], latest['ceiling']
        baseline = cursor < ceiling
        if baseline:
            rows = db.execute('SELECT a.id,a.revision,substr(k.body,1,?) AS body FROM active a LEFT JOIN knowledge k '
                              'ON k.project=a.project AND k.id=a.id AND k.revision=a.revision '
                              'WHERE a.project=? AND a.id>? AND a.id<=? ORDER BY a.id LIMIT ?',
                              (MAX_BODY + 1, project, cursor, ceiling, max_items + 1)).fetchall()
        else:
            rows = db.execute('SELECT p.knowledge_id AS id,a.revision,substr(k.body,1,?) AS body '
                              'FROM knowledge_health_pending p LEFT JOIN active a '
                              'ON a.project=p.project AND a.id=p.knowledge_id LEFT JOIN knowledge k '
                              'ON k.project=a.project AND k.id=a.id AND k.revision=a.revision '
                              'WHERE p.project=? ORDER BY p.knowledge_id LIMIT ?',
                              (MAX_BODY + 1, project, max_items)).fetchall()
        counts = Counter(json.loads(latest['issue_counts']))
        processed, scanned_delta, affected_delta = 0, 0, 0
        for row in rows[:max_items]:
            expired = time.monotonic() >= deadline
            if processed and expired:
                break
            previous = db.execute('SELECT issues FROM knowledge_health_items '
                                  'WHERE project=? AND rule_version=? AND knowledge_id=?',
                                  (project, RULE_VERSION, row['id'])).fetchone()
            if previous:
                prior_issues = json.loads(previous['issues'])
                counts.subtract(prior_issues)
                affected_delta -= bool(prior_issues)
            if row['revision'] is None:
                if previous:
                    db.execute('DELETE FROM knowledge_health_items WHERE project=? AND rule_version=? AND knowledge_id=?',
                               (project, RULE_VERSION, row['id']))
                    scanned_delta -= 1
            else:
                try:
                    # Only bounded read queries may be interrupted. Never
                    # interrupt a checkpoint write and lose the completed prefix.
                    if expired:
                        title, issues = '', ['check_budget_partial']
                    else:
                        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                        title, issues = _issues(runtime, project, row)
                except sqlite3.OperationalError as exc:
                    if 'interrupted' not in str(exc).lower() or not db.in_transaction:
                        raise
                    title, issues = '', ['check_budget_partial']
                finally:
                    db.set_progress_handler(None, 0)
                db.execute('INSERT INTO knowledge_health_items VALUES(?,?,?,?,?,?,?) '
                           'ON CONFLICT(project,rule_version,knowledge_id) DO UPDATE SET revision=excluded.revision, '
                           'title=excluded.title,issues=excluded.issues,checked=excluded.checked',
                           (project, RULE_VERSION, row['id'], row['revision'], title,
                            json.dumps(issues, ensure_ascii=False), time.time()))
                counts.update(issues)
                affected_delta += bool(issues)
                scanned_delta += previous is None
            db.execute('DELETE FROM knowledge_health_pending WHERE project=? AND knowledge_id=?', (project, row['id']))
            if baseline:
                cursor = row['id']
            processed += 1
        if baseline and processed == len(rows):
            cursor = ceiling
        done = cursor == ceiling and not _has_pending(db, project)
        now = time.time()
        db.execute('UPDATE knowledge_health_runs SET state=?,cursor=?,scanned=scanned+?,affected=affected+?, '
                   'issue_counts=?,updated=?,completed=?,generation=?,revocation_epoch=? WHERE project=? AND rule_version=?',
                   ('completed' if done else 'checking', cursor, scanned_delta, affected_delta,
                    json.dumps(dict(+counts)), now, now if done else None,
                    manifest['generation'] if done else latest['generation'],
                    manifest['revocation_epoch'] if done else latest['revocation_epoch'], project, RULE_VERSION))
        db.commit()
        return {'state': 'completed' if done else 'checking', 'advanced': processed, 'rule_version': RULE_VERSION}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.set_progress_handler(None, 0)
        db.execute('PRAGMA busy_timeout=' + str(previous_timeout))


def maybe_advance(runtime, project):
    """Diagnostics must never turn a working project/tool into a blocked one."""
    try:
        result = advance(runtime, project)
    except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        result = {'state': 'deferred', 'reason': 'health_check_unavailable'}
    runtime._knowledge_health_observation = result
    return result


def report(db, project, *, offset=0, limit=20):
    try:
        return _report(db, project, offset=offset, limit=limit)
    except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return {'project_id': project, 'expected_rule_version': RULE_VERSION, 'rule_version': None,
                'state': 'unavailable', 'scanned': None, 'affected': None, 'issue_counts': {}, 'items': [],
                'started': None, 'completed': None, 'snapshot_current': False, 'next_offset': None,
                'reason': 'health_report_unavailable'}


def _report(db, project, *, offset=0, limit=20):
    """Read only cached observations. No full-source expansion or automatic scan."""
    if type(offset) is not int or type(limit) is not int or not 0 <= offset <= 10000 or not 0 <= limit <= 50:
        raise ValueError('invalid knowledge health page')
    result = {'project_id': project, 'expected_rule_version': RULE_VERSION, 'rule_version': None,
              'state': 'not_started', 'scanned': 0, 'affected': 0, 'issue_counts': {}, 'items': [],
              'started': None, 'completed': None, 'snapshot_current': False, 'next_offset': None,
              'snapshot_generation': None, 'current_generation': None,
              'source_freshness': 'observed_at_check_time', 'snapshot_scope': 'publication_manifest_and_recorded_source_changes',
              'scope': 'active_formal_structural_and_direct_evidence', 'semantic_reclassified': False}
    try:
        row = db.execute('SELECT * FROM knowledge_health_runs WHERE project=? ORDER BY rule_version DESC LIMIT 1',
                         (project,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc).lower():
            return result
        raise
    if row is None:
        return result
    state = _project_manifest(db, project)
    expected_rule = row['rule_version'] == RULE_VERSION
    result.update({key: row[key] for key in ('rule_version', 'state', 'scanned', 'affected', 'started', 'completed')})
    result.update(issue_counts=json.loads(row['issue_counts']), snapshot_generation=row['generation'],
                  current_generation=state['generation'] if state else None,
                  snapshot_current=expected_rule and _covers_project(row, state) and not _has_pending(db, project))
    if limit:
        pending = ("EXISTS(SELECT 1 FROM knowledge_health_pending p WHERE p.project=h.project "
                   "AND p.knowledge_id=h.knowledge_id)" if expected_rule else "0")
        rows = db.execute("SELECT h.*,a.revision AS active_revision," + pending + " AS pending "
                          "FROM knowledge_health_items h LEFT JOIN active a "
                          "ON a.project=h.project AND a.id=h.knowledge_id WHERE h.project=? AND h.rule_version=? "
                          "AND h.issues!='[]' ORDER BY h.knowledge_id LIMIT ? OFFSET ?",
                          (project, row['rule_version'], limit + 1, offset)).fetchall()
        result['items'] = [{'knowledge_id': r['knowledge_id'], 'revision': r['revision'], 'title': r['title'],
                            'issues': json.loads(r['issues']), 'checked': r['checked'],
                            'current': expected_rule and r['active_revision'] == r['revision'] and not r['pending']}
                           for r in rows[:limit]]
        result['next_offset'] = offset + limit if len(rows) > limit else None
    return result
