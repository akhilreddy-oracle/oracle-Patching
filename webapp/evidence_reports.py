"""Evidence-backed comparisons. Unknown/missing evidence never becomes a pass."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone

import execution_console
import planctl
from diagnostics import redact_text, redacted

_METRICS = {'binary_inventory': 'Binary patch inventory', 'sql_patch_status': 'Target SQL patch status',
            'invalid_objects': 'Invalid objects', 'components': 'Database components',
            'listener_ready': 'Listener exposes target as READY', 'database_state': 'Database state'}

# These are the native dispatch stages, not labels invented by the report.
_FINAL_STAGES = {
    'final_validate', 'rollback_final_validate',
    'cluster_final_validate', 'rac_rollback_final_validate',
    'grid_cluster_final_validate', 'grid_rollback_cluster_final_validate',
    'opatchauto_cluster_final_validate', 'opatchauto_rollback_cluster_final_validate',
    'ojvm_final_validate', 'ojvm_rollback_final_validate',
    'oop_final_validate', 'oop_switchback_final_validate',
}


def _checked(path, expected, maximum=64 * 1024 * 1024):
    path = Path(path)
    if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('source has no valid SHA-256 binding')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError('source is not a bounded independent regular file')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(maximum + 1)
        after = os.fstat(fd)
        named = path.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('source changed while being verified')
        if len(data) > maximum or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('source SHA-256 does not match the sealed reference')
        return data
    finally:
        os.close(fd)


def _unknown():
    return {key: {'status': 'unknown', 'value': None, 'reason': 'No verified evidence for this observation'} for key in _METRICS}


def _set(facts, key, value, source):
    if value is not None:
        facts[key] = {'status': 'verified', 'value': value, 'source': source}


def _snapshot_facts(document, target, source, facts):
    if not isinstance(document, dict):
        raise ValueError('snapshot is not an object')
    homes = [home for home in document.get('oracle_homes') or [] if isinstance(home, dict) and home.get('path') == target.get('oracle_home')]
    if len(homes) == 1 and isinstance(homes[0].get('patches'), list):
        _set(facts, 'binary_inventory', sorted(str(item) for item in homes[0]['patches']), source)
    databases = [database for database in document.get('databases') or []
                 if isinstance(database, dict) and database.get('db_unique_name') == target.get('database_unique_name')
                 and database.get('oracle_home') == target.get('oracle_home')]
    if len(databases) == 1:
        runtime = databases[0].get('runtime') if isinstance(databases[0].get('runtime'), dict) else {}
        if runtime.get('status') == 'complete':
            invalid = runtime.get('invalid_objects')
            if type(invalid) is int and invalid >= 0:
                _set(facts, 'invalid_objects', invalid, source)
            values = [runtime.get(key) for key in ('database_role', 'open_mode', 'instance_state')]
            if all(isinstance(value, str) and value for value in values):
                _set(facts, 'database_state', ' / '.join(values), source)


def _text_facts(name, text, source, facts, target):
    # Only specific custodied native artifacts carry these facts. Generic
    # stdout/error text is never interpreted as a successful health check.
    if name.endswith('lspatches.log'):
        patches = re.findall(r'(?m)^([0-9]{1,20});', text)
        if patches:
            _set(facts, 'binary_inventory', sorted(set(patches)), source)
    values = dict(re.findall(r'(?m)^([A-Z_]+)=(.*?)\s*$', text))
    if name.endswith('health.log'):
        if values.get('DATABASE_UNIQUE_NAME') != target.get('database_unique_name'):
            return
        if values.get('INVALID_OBJECTS', '').isdigit():
            _set(facts, 'invalid_objects', int(values['INVALID_OBJECTS']), source)
        state = [values.get(key) for key in ('DATABASE_ROLE', 'OPEN_MODE', 'INSTANCE_STATUS')]
        if all(state):
            _set(facts, 'database_state', ' / '.join(state), source)
    if 'dictionary' in name:
        if values.get('INVALID_OBJECTS', '').isdigit():
            _set(facts, 'invalid_objects', int(values['INVALID_OBJECTS']), source)
        components = dict(re.findall(r'(?m)^COMPONENT=([A-Za-z0-9_]+)\|([^\r\n]+)$', text))
        if components:
            _set(facts, 'components', components, source)
    if 'sqlpatch' in name:
        action, status = values.get('SQLPATCH_LATEST_ACTION'), values.get('SQLPATCH_LATEST_STATUS')
        if action and status:
            _set(facts, 'sql_patch_status', f'{action}/{status}', source)
    if name.endswith('listener.log'):
        sid = target.get('oracle_sid') or target.get('database_unique_name')
        if re.search(r'Instance "?' + re.escape(str(sid)) + r'"?, status READY', text):
            _set(facts, 'listener_ready', True, source)


def build(plan_id):
    plan = planctl.status(plan_id)
    target = plan.get('target') or {}
    before, after = _unknown(), _unknown()
    sources, gaps, evidence_records = [], [], []
    references = []
    reconciliation = (plan.get('source_documents') or {}).get('reconciliation')
    if isinstance(reconciliation, dict):
        references.append(('pre-plan reconciliation', reconciliation))
    references += [('pre-plan discovery', ref) for ref in plan.get('snapshot_evidence') or [] if isinstance(ref, dict)]
    for label, ref in references:
        source = {'label': label, 'path': ref.get('path'), 'sha256': ref.get('sha256')}
        try:
            document = json.loads(_checked(source['path'], source['sha256'], 4 * 1024 * 1024))
            _snapshot_facts(document, target, source, before)
            sources.append({**source, 'integrity': 'verified'})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            gaps.append({'source': label, 'reason': redact_text(str(exc), 400)})
    task_rows = execution_console.tasks(plan_id)
    checked_tasks = set()
    for task in task_rows:
        if not task.get('evidence_verified'):
            if task.get('verification_error'):
                gaps.append({'source': task['task_id'], 'reason': task['verification_error']})
            continue
        generation = task.get('retry_count', 0)
        if type(generation) is not int or not 0 <= generation <= 1000000:
            gaps.append({'source': task['task_id'], 'reason': 'Invalid attempt generation'})
            continue
        leaf = task['task_id'] + (f'-retry{generation}' if generation else '')
        directory = planctl.PLAN_STATE_DIR / 'plans' / plan_id / 'evidence' / leaf
        try:
            custody = task.get('evidence_custody') or {}
            manifest = json.loads(_checked(directory / 'custody.json', custody.get('manifest_sha256')))
            native = json.loads(_checked(directory / 'evidence.json', task.get('evidence_sha256')))
            if not isinstance(manifest, dict) or not isinstance(native, dict):
                raise ValueError('native evidence and custody must be objects')
            if (native.get('plan_id') != plan_id or native.get('plan_sha256') != plan['plan_sha256']
                    or native.get('task_id') != task['task_id'] or native.get('status') != task.get('status')
                    or native.get('stage') != task.get('stage')):
                raise ValueError('native evidence scope differs from its verified task')
            native_target = native.get('target') or {}
            if not isinstance(native_target, dict) or any(target.get(key) and native_target.get(key) != target[key] for key in ('database_unique_name', 'oracle_home', 'grid_home')):
                raise ValueError('native evidence targets another database/home')
            # Failed tasks retain diagnostics but cannot overwrite healthy facts
            # with incomplete checks or pre-failure console output.
            stage = task.get('stage') or ''
            destination = before if stage == 'precheck' or stage.endswith('_precheck') else after
            task_facts, task_sources, failure_diagnostics = dict(destination), [], []
            for item in manifest.get('files') or []:
                if not isinstance(item, dict):
                    raise ValueError('invalid custody file entry')
                name = Path(item.get('custody_path') or '').name
                if not name or name in {'.', '..'}:
                    continue
                source = {'label': f"{task['task_id']} / {name}", 'path': str(directory / name),
                          'sha256': item.get('sha256'), 'task_id': task['task_id'], 'stage': native.get('stage'),
                          'evidence_finished_at': native.get('finished_at')}
                raw = _checked(directory / name, source['sha256'])
                task_sources.append({**source, 'integrity': 'verified'})
                if task.get('status') != 'succeeded':
                    if item.get('role') in {'stdout', 'stderr'} and raw.strip():
                        failure_diagnostics.append({'source': source, 'stream': item['role'],
                                                    'text': redact_text(raw.decode('utf-8', 'replace'), 4000)})
                    continue
                if name in {'report-before.json', 'report-after.json'}:
                    document = json.loads(raw)
                    if not isinstance(document, dict) or not isinstance(document.get('target'), dict) or document['target'].get('database_unique_name') != target.get('database_unique_name') or document['target'].get('oracle_home') != target.get('oracle_home'):
                        raise ValueError('reporting observation targets another database/home')
                    for key in _METRICS:
                        _set(task_facts, key, (document.get('observations') or {}).get(key), source)
                elif item.get('role') == 'artifact':
                    _text_facts(name, raw.decode('utf-8', 'replace'), source, task_facts, {**target, **native_target})
            destination.update(task_facts)
            sources.extend(task_sources)
            record = {key: native.get(key) for key in ('plan_id', 'task_id', 'plan_sha256', 'record_sha256', 'stage',
                      'status', 'postcondition', 'actor', 'started_at', 'finished_at', 'exit_code', 'outcome_class', 'target', 'patch')}
            if task.get('status') == 'failed':
                record['failure_diagnostics'] = failure_diagnostics
                extjob = any('extjob ownership/mode differs from README:' in item['text'] for item in failure_diagnostics)
                record['next_action'] = (
                    'Have an administrator verify the Oracle binary provenance and the README-required ownership and mode before considering a supported repair. Preserve the completed apply and SQL patch evidence. This report does not authorize a permission change.'
                    if extjob else 'Inspect the saved failure and correct its cause. Retry requires an open maintenance window and a native outcome that is safe to repeat; an unknown outcome needs reconciliation first.')
            evidence_records.append(record)
            checked_tasks.add(task['task_id'])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            gaps.append({'source': task.get('task_id'), 'reason': redact_text(str(exc), 400)})
    final = [task for task in task_rows if task.get('stage') in _FINAL_STAGES]
    source_complete = plan.get('state') == 'succeeded' and len(final) == 1 and final[0].get('status') == 'succeeded' and final[0].get('evidence_verified') is True and final[0].get('task_id') in checked_tasks
    rollback = {'status': 'requires_native_validation' if source_complete and plan.get('intent', 'patch_apply') == 'patch_apply' else 'not_available',
                'approved': False, 'native_validation': 'not_run',
                'reason': ('Source final evidence is verified. Creating a separate rollback request must still pass the native lineage, README, recovery and approval checks.' if source_complete
                           else 'A successful source plan and verified final validation are required before a rollback request can be evaluated.')}
    comparison = []
    for key, label in _METRICS.items():
        comparison.append({'metric': key, 'label': label, 'before': before[key], 'after': after[key],
                           'changed': before[key]['value'] != after[key]['value'] if before[key]['status'] == after[key]['status'] == 'verified' else None})
    report = {'schema_version': '1.0', 'plan_id': plan_id, 'plan_sha256': plan['plan_sha256'], 'state': plan.get('state'),
              'patch_id': plan.get('patch_id'), 'intent': plan.get('intent', 'patch_apply'), 'target': target,
              'maintenance_window': plan.get('maintenance_window'), 'generated_at': datetime.now(timezone.utc).isoformat(),
              'completion_verified': source_complete, 'comparison': comparison, 'verified_sources': sources,
              'evidence': {'tasks': evidence_records}, 'gaps': gaps, 'rollback': rollback,
              'interpretation': 'Verified means the saved evidence and its custody matched their sealed hashes. It is not a fresh live-health check. Unknown means evidence is absent or unverifiable.',
              'after_scope': 'verified final validation' if source_complete else 'latest verified completed stages; final validation is incomplete'}
    report = redacted(report)
    report['report_sha256'] = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return report


def _value(fact):
    if fact.get('status') != 'verified':
        return 'Unknown'
    value = fact.get('value')
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list, bool)) else str(value)


def export(report, format):
    """Return (text, content_type, safe filename); exported values are escaped."""
    report = redacted(report)
    stem = re.sub(r'[^A-Za-z0-9_.-]', '_', str(report.get('plan_id') or 'plan')) + '-evidence'
    if format == 'json':
        return json.dumps(report, indent=2, ensure_ascii=True) + '\n', 'application/json; charset=utf-8', stem + '.json'
    if format == 'csv':
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['Plan', 'Patch', 'Metric', 'Before', 'Before integrity', 'After', 'After integrity', 'After evidence'])
        def cell(value):
            value = str(value)
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r')) else value
        for row in report.get('comparison', []):
            writer.writerow([cell(report.get('plan_id')), cell(report.get('patch_id')), cell(row['label']), cell(_value(row['before'])),
                             row['before']['status'], cell(_value(row['after'])), row['after']['status'], cell(row['after'].get('source', {}).get('label', ''))])
        return output.getvalue(), 'text/csv; charset=utf-8', stem + '.csv'
    if format != 'html':
        raise ValueError('report format must be json, html or csv')
    escape = lambda value: html.escape(str(value), quote=True)
    rows = ''.join('<tr><th>' + escape(row['label']) + '</th><td>' + escape(_value(row['before'])) + '</td><td>' + escape(_value(row['after']))
                   + '</td><td>' + escape(row['after'].get('source', {}).get('label', 'No verified evidence')) + '</td></tr>' for row in report.get('comparison', []))
    gaps = ''.join('<li>' + escape(item.get('source')) + ': ' + escape(item.get('reason')) + '</li>' for item in report.get('gaps', []))
    failures = ''.join('<section><h3>' + escape(task.get('task_id')) + '</h3><p>' + escape((task.get('postcondition') or {}).get('detail', 'Task failed'))
                      + '</p>' + ''.join('<pre>' + escape(item.get('text')) + '</pre>' for item in task.get('failure_diagnostics', []))
                      + '<p>' + escape(task.get('next_action', 'Review the saved evidence before retrying.')) + '</p></section>'
                      for task in (report.get('evidence') or {}).get('tasks', []) if task.get('status') == 'failed')
    body = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>' + escape(stem) + '</title><style>'
            'body{font:15px system-ui,sans-serif;max-width:1100px;margin:40px auto;color:#20252b}table{border-collapse:collapse;width:100%}'
            'th,td{border:1px solid #c8ced4;text-align:left;padding:10px;overflow-wrap:anywhere}th{background:#f0f3f5}'
            'p{line-height:1.6}.meta{color:#53606d}@media print{body{margin:0;font-size:10pt}tr{break-inside:avoid}}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}'
            '</style><h1>Patch evidence report</h1><p><strong>' + escape(report.get('plan_id')) + '</strong> · Patch ' + escape(report.get('patch_id'))
            + ' · ' + escape(report.get('state')) + '</p><p class="meta">Generated ' + escape(report.get('generated_at')) + '<br>After: '
            + escape(report.get('after_scope')) + '</p><table><thead><tr><th>Check</th><th>Before</th><th>After</th><th>Evidence</th></tr></thead><tbody>'
            + rows + '</tbody></table><p>' + escape(report.get('interpretation')) + '</p><h2>Rollback</h2><p>'
            + escape(report.get('rollback', {}).get('reason')) + '</p>' + ('<h2>Evidence gaps</h2><ul>' + gaps + '</ul>' if gaps else '')
            + ('<h2>Saved failure diagnostics</h2>' + failures if failures else '')
            + '<p class="meta">Plan SHA-256: ' + escape(report.get('plan_sha256')) + '<br>Report SHA-256: ' + escape(report.get('report_sha256')) + '</p></html>')
    return body, 'text/html; charset=utf-8', stem + '.html'
