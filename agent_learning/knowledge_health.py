"""Bounded, disposable knowledge diagnostics shared by all host installations.

These observations never change knowledge, enqueue reviews or prove truth.
The installer does not open a database; the first bound project advances it.
"""
from __future__ import annotations

from collections import Counter
import json
import sqlite3
import time

RULE_VERSION = 1
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


def advance(runtime, project, *, max_items=12, max_seconds=.075):
    """One short atomic page. No long lease, host key, model or persistent worker."""
    if type(max_items) is not int or not 1 <= max_items <= 200 or not 0 < max_seconds <= 2:
        raise ValueError('invalid knowledge health batch budget')
    db = runtime.db
    if db.in_transaction:
        return {'state': 'deferred', 'reason': 'outer_transaction'}
    latest = db.execute('SELECT rule_version,state FROM knowledge_health_runs WHERE project=? '
                        'ORDER BY rule_version DESC LIMIT 1', (project,)).fetchone()
    if latest and (latest[0] > RULE_VERSION or latest[0] == RULE_VERSION and latest[1] == 'completed'):
        return {'state': latest['state'], 'advanced': 0, 'rule_version': latest[0]}
    previous_timeout = db.execute('PRAGMA busy_timeout').fetchone()[0]
    deadline = time.monotonic() + max_seconds
    try:
        db.execute('PRAGMA busy_timeout=0')
        db.execute('BEGIN IMMEDIATE')
        # Recheck after acquiring the shared writer lock: another host may have finished.
        latest = db.execute('SELECT * FROM knowledge_health_runs WHERE project=? '
                            'ORDER BY rule_version DESC LIMIT 1', (project,)).fetchone()
        if latest and (latest['rule_version'] > RULE_VERSION or
                       latest['rule_version'] == RULE_VERSION and latest['state'] == 'completed'):
            db.commit()
            return {'state': latest['state'], 'advanced': 0, 'rule_version': latest['rule_version']}
        if not latest or latest['rule_version'] < RULE_VERSION:
            state = db.execute('SELECT generation,revocation_epoch FROM projects WHERE id=?', (project,)).fetchone()
            if state is None:
                raise ValueError('unknown knowledge health project')
            last = db.execute('SELECT id FROM active WHERE project=? ORDER BY id DESC LIMIT 1', (project,)).fetchone()
            now = time.time()
            db.execute('INSERT INTO knowledge_health_runs VALUES(?,?,?, ?,?,0,0,?, ?,?,?,?,NULL)',
                       (project, RULE_VERSION, 'checking', '', last[0] if last else '', '{}', state[0], state[1], now, now))
            latest = db.execute('SELECT * FROM knowledge_health_runs WHERE project=? AND rule_version=?',
                                (project, RULE_VERSION)).fetchone()
        rows = db.execute('SELECT a.id,a.revision,substr(k.body,1,?) AS body FROM active a LEFT JOIN knowledge k '
                          'ON k.project=a.project AND k.id=a.id AND k.revision=a.revision '
                          'WHERE a.project=? AND a.id>? AND a.id<=? ORDER BY a.id LIMIT ?',
                          (MAX_BODY + 1, project, latest['cursor'], latest['ceiling'], max_items + 1)).fetchall()
        counts, cursor, processed, affected = Counter(json.loads(latest['issue_counts'])), latest['cursor'], 0, 0
        for row in rows[:max_items]:
            if time.monotonic() >= deadline:
                if processed:
                    break
                title, issues = '', ['check_budget_partial']
            else:
                try:
                    # Only bounded read queries may be interrupted. Never
                    # interrupt a checkpoint write and lose the completed prefix.
                    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                    title, issues = _issues(runtime, project, row)
                except sqlite3.OperationalError as exc:
                    if 'interrupted' not in str(exc).lower() or not db.in_transaction:
                        raise
                    title, issues = '', ['check_budget_partial']
                finally:
                    db.set_progress_handler(None, 0)
            db.execute('INSERT INTO knowledge_health_items VALUES(?,?,?,?,?,?,?)',
                       (project, RULE_VERSION, row['id'], row['revision'], title,
                        json.dumps(issues, ensure_ascii=False), time.time()))
            counts.update(issues)
            cursor, processed, affected = row['id'], processed + 1, affected + bool(issues)
        done = processed == len(rows)
        now = time.time()
        db.execute('UPDATE knowledge_health_runs SET state=?,cursor=?,scanned=scanned+?,affected=affected+?, '
                   'issue_counts=?,updated=?,completed=? WHERE project=? AND rule_version=?',
                   ('completed' if done else 'checking', cursor, processed, affected,
                    json.dumps(dict(counts)), now, now if done else None, project, RULE_VERSION))
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
              'source_freshness': 'observed_at_check_time', 'snapshot_scope': 'publication_manifest_only',
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
    state = db.execute('SELECT generation,revocation_epoch FROM projects WHERE id=?', (project,)).fetchone()
    result.update({key: row[key] for key in ('rule_version', 'state', 'scanned', 'affected', 'started', 'completed')})
    result.update(issue_counts=json.loads(row['issue_counts']), snapshot_generation=row['generation'],
                  current_generation=state[0] if state else None,
                  snapshot_current=bool(state and state[0] == row['generation'] and state[1] == row['revocation_epoch']))
    if limit:
        rows = db.execute("SELECT h.*,a.revision AS active_revision FROM knowledge_health_items h LEFT JOIN active a "
                          "ON a.project=h.project AND a.id=h.knowledge_id WHERE h.project=? AND h.rule_version=? "
                          "AND h.issues!='[]' ORDER BY h.knowledge_id LIMIT ? OFFSET ?",
                          (project, row['rule_version'], limit + 1, offset)).fetchall()
        result['items'] = [{'knowledge_id': r['knowledge_id'], 'revision': r['revision'], 'title': r['title'],
                            'issues': json.loads(r['issues']), 'checked': r['checked'],
                            'current': r['active_revision'] == r['revision']} for r in rows[:limit]]
        result['next_offset'] = offset + limit if len(rows) > limit else None
    return result
