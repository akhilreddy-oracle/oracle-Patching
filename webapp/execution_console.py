"""Persistent execution views and bounded inspection of an existing launch.

There is deliberately no tools sync, launch, resume, state reconciliation,
free-form command, or caller-selected pathname in this module.
"""
from __future__ import annotations

import json
import re
import time

import pipeline_runner
import planctl
import remote
from diagnostics import redact_text, redacted

_RUN = re.compile(r'[a-f0-9]{12}\Z')
_REMOTE_RUN = re.compile(r'[a-f0-9]{32}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
_NATIVE_ROOTS = {'database_single_instance_opatch': 'single-instance',
                 'database_single_instance_opatch_rollback': 'single-instance-rollback'}


# Executed as fixed argv by the existing SSH connector. Python 3.6 compatible
# for Oracle Linux system interpreters. Inputs are derived from persisted scope.
_READ_EXISTING = r'''
import hashlib,json,os,re,stat,sys,time
root,plan,task,launch,definition,generation,adapter_root=sys.argv[1:]
ident=re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')
assert ident.fullmatch(plan) and ident.fullmatch(task)
assert re.fullmatch(r'[a-f0-9]{32}',launch)
attempt_bound=bool(definition or generation)
assert not attempt_bound or (re.fullmatch(r'[a-f0-9]{64}',definition) and generation.isdigit() and 0 <= int(generation) <= 1000000)
assert adapter_root in ('','single-instance','single-instance-rollback')
assert os.path.isabs(root) and os.path.realpath(root)==root
wrapper=os.path.join(root,'var','webapp-runs',plan,task,launch)
def safe_read(path,limit=65536,tail=False):
    parts=path.split('/')[1:]; cursor=''
    for part in parts[:-1]:
        cursor+='/'+part
        if stat.S_ISLNK(os.lstat(cursor).st_mode): raise ValueError('symbolic link directory rejected')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise ValueError('unsafe diagnostic file')
        if not tail and info.st_size>limit: raise ValueError('record exceeds size bound')
        offset=max(0,info.st_size-limit) if tail else 0
        os.lseek(fd,offset,os.SEEK_SET); data=os.read(fd,limit)
        if offset:
            boundary=data.find(b'\n')
            data=data[boundary+1:] if boundary>=0 else b'[Oversized line omitted]\n'
        return data.decode('utf-8','replace'),{'bytes':info.st_size,'truncated':bool(offset),'modified_at':info.st_mtime}
    finally: os.close(fd)
def optional(path,tail=False):
    try: return safe_read(path,tail=tail)
    except (OSError,ValueError) as exc: return None,{'available':False,'reason':str(exc)}
logs={}
for name in ('stdout','stderr'):
    text,meta=optional(os.path.join(wrapper,name),True)
    logs['wrapper_'+name]=dict(meta,text=text)
rc,_=optional(os.path.join(wrapper,'rc'))
pid,_=optional(os.path.join(wrapper,'pid'))
wrapper_state={'exit_code':int(rc.strip()) if rc and re.fullmatch(r'-?\d+',rc.strip()) else None}
if pid and pid.strip().isdigit():
    wrapper_state['process_observation']={'pid':int(pid.strip()),'present':os.path.exists('/proc/'+pid.strip()),'identity_verified':False}
heartbeat={'source':'native_task_lease_observation','available':False,'verified_terminal':False,'attempt_bound':attempt_bound}
task_text=None
if attempt_bound:
    task_text,_=optional(os.path.join(root,'var','webapp-plans','plans',plan,'tasks',task+'.json'))
if task_text:
    native=json.loads(task_text)
    keys=('schema_version','task_id','plan_id','plan_sha256','stage','node','adapter','authorization_sha256','authorization_record_sha256')
    digest=hashlib.sha256(json.dumps({key:native.get(key) for key in keys},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    if native.get('task_id')!=task or native.get('plan_id')!=plan or digest!=definition or native.get('task_definition_sha256')!=definition or native.get('retry_count',0)!=int(generation):
        raise ValueError('native task identity changed')
    for key in ('lease_renewed_at_epoch','claimed_at_epoch','lease_expires_epoch'):
        if native.get(key) is not None and (type(native[key]) is not int or native[key]<0): raise ValueError('invalid native lease')
    heartbeat.update(available=True,task_status=native.get('status'),last_renewed_at=native.get('lease_renewed_at_epoch'),claimed_at=native.get('claimed_at_epoch'),lease_expires_at=native.get('lease_expires_epoch'))
    if adapter_root:
        leaf=task+('-retry'+generation if int(generation) else '')
        native_dir='/var/lib/oracle-patching-utility/'+adapter_root+'/plans/'+plan+'/tasks/'+leaf
        for name in ('stdout.log','stderr.log','datapatch.log','utlrp-console.log'):
            text,meta=optional(os.path.join(native_dir,name),True)
            logs[name]=dict(meta,text=text)
print(json.dumps({'schema_version':'1.0','plan_id':plan,'task_id':task,'remote_launch_id':launch,'observed_at':time.time(),'remote_clock':time.time(),'worker_heartbeat':heartbeat,'wrapper':wrapper_state,'logs':logs,'read_only':True,'terminal_result_verified':False}))
'''


def tasks(plan_id):
    planctl.validate_plan_id(plan_id)
    rows = []
    for path in sorted((planctl.PLAN_STATE_DIR / 'plans' / plan_id / 'tasks').glob('*.json')):
        if not _ID.fullmatch(path.stem):
            continue
        try:
            row = planctl._run(['task-status', '--plan-id', plan_id, '--task-id', path.stem])
            row['evidence_verified'] = row.get('status') in {'succeeded', 'failed'}
        except (planctl.PlanError, OSError, ValueError) as exc:
            row = {'task_id': path.stem, 'status': 'unknown', 'evidence_verified': False,
                   'verification_error': redact_text(str(exc), 600)}
        rows.append(row)
    return rows


def _runs(plan_id):
    return [record for item in reversed(pipeline_runner.list_runs(f'plan:{plan_id}:')[:100])
            if (record := pipeline_runner.get_run(item['run_id'])) is not None]


def snapshot(plan_id):
    plan = planctl.status(plan_id)
    rows = tasks(plan_id)
    runs = []
    timeline = []
    for record in _runs(plan_id):
        data = record.to_json()
        # Native signed result bodies can be large. This view exports only
        # bounded presentation data, never credentials or arbitrary result JSON.
        item = {key: data.get(key) for key in ('run_id', 'kind', 'status', 'created_at', 'started_at', 'finished_at',
                                              'elapsed_seconds', 'controller_poll', 'observation', 'log_tail')}
        context = data.get('context') or {}
        item['task_id'] = context.get('task_id')
        detached = data.get('key') == f'plan:{plan_id}:execute' and context.get('detached_execution') is True
        binding = context.get('execution_host_configuration_sha256')
        bound = isinstance(binding, str) and re.fullmatch(r'[a-f0-9]{64}', binding) is not None
        item['can_observe'] = detached and bound
        if detached and not bound:
            item['observe_blocked_reason'] = ('This launch has no verified host configuration binding. '
                'Only saved logs are available. Preserve its unresolved outcome and inspect the original launch configuration before reconciliation.')
        item['error'] = redact_text((data.get('error') or {}).get('message') if isinstance(data.get('error'), dict) else '', 800)
        runs.append(redacted(item))
        for event in data.get('timeline') or []:
            if isinstance(event, dict):
                timeline.append({**redacted(event), 'run_id': data['run_id']})
    unresolved = any(run['status'] in {'unknown', 'reconciling'} and
                     (run['can_observe'] or run.get('observe_blocked_reason')) for run in runs)
    guidance = ('Inspect and reconcile the existing execution before starting another task.' if unresolved else
                'A task failed or is blocked. Inspect verified evidence before using the existing retry workflow.' if plan.get('state') in {'paused', 'failed'} else
                'Controller polling shows connectivity; the native lease observation shows worker heartbeat freshness. Neither proves task completion.')
    return {'schema_version': '1.0', 'plan_id': plan_id, 'plan_sha256': plan['plan_sha256'], 'state': plan.get('state'),
            'tasks': [{key: task.get(key) for key in ('task_id', 'stage', 'node', 'status', 'claimed_at_epoch', 'completed_at_epoch',
                                                    'lease_renewed_at_epoch', 'lease_expires_epoch', 'evidence_verified', 'verification_error')} for task in rows],
            'runs': runs, 'timeline': sorted(timeline, key=lambda item: item.get('at') or 0)[-300:],
            'guidance': guidance, 'observed_at': time.time(), 'read_only': True}


def observe(plan_id, run_id):
    planctl.validate_plan_id(plan_id)
    if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
        raise planctl.PlanError('invalid existing run ID')
    record = pipeline_runner.get_run(run_id)
    if record is None or record.key != f'plan:{plan_id}:execute':
        raise planctl.PlanError('run does not belong to this plan execution')
    context = dict(record.context)
    task_id = context.get('task_id')
    if context.get('plan_id') != plan_id or context.get('detached_execution') is not True or not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        raise planctl.PlanError('run has no valid persisted detached launch')
    planctl.status(plan_id)  # Verify the local immutable plan before remote reads.
    task = planctl._run(['task-status', '--plan-id', plan_id, '--task-id', task_id])
    host = planctl.resolve_bound_execution_host(context)
    prefix = planctl._remote_run_dir(host, plan_id, task_id) + '/'
    path = context.get('remote_run_dir')
    if not isinstance(path, str) or not path.startswith(prefix) or not _REMOTE_RUN.fullmatch(path[len(prefix):]):
        raise planctl.PlanError('persisted remote launch path is invalid')
    # A host-bound launch may predate the persisted attempt binding. Its exact
    # wrapper output remains observable, but never attach a later retry's
    # native logs/lease to that earlier execution. Unbound legacy hosts were
    # already rejected before any remote read.
    definition, generation = context.get('task_definition_sha256'), context.get('task_retry_count')
    attempt_bound = definition is not None or generation is not None
    if attempt_bound:
        if not isinstance(definition, str) or not re.fullmatch(r'[a-f0-9]{64}', definition) or type(generation) is not int or not 0 <= generation <= 1000000:
            raise planctl.PlanError('persisted task generation is invalid')
        if task.get('task_definition_sha256') != definition or task.get('retry_count', 0) != generation:
            raise planctl.PlanError('task attempt changed since this execution; only its saved observations remain available')
    argv = ['/usr/bin/env', 'python3', '-c', _READ_EXISTING, host['remote_root'], plan_id, task_id,
            path[len(prefix):], definition if attempt_bound else '', str(generation) if attempt_bound else '',
            _NATIVE_ROOTS.get(task.get('adapter'), '') if attempt_bound else '']
    result = remote.run_remote_raw(host['ssh_alias'], argv, timeout=30, sudo=bool(host.get('sudo')))
    if result.returncode != 0:
        raise planctl.PlanError('existing-run inspection could not complete', stderr=redact_text(result.stderr, 1000))
    if len(result.stdout) > 1024 * 1024:
        raise planctl.PlanError('diagnostic response exceeds the output bound')
    payload = json.loads(result.stdout)
    if (not isinstance(payload, dict) or payload.get('plan_id') != plan_id or payload.get('task_id') != task_id
            or payload.get('remote_launch_id') != path[len(prefix):] or payload.get('read_only') is not True):
        raise planctl.PlanError('diagnostic response differs from the existing execution')
    payload = redacted(payload)
    payload['run_id'] = run_id
    payload['received_at'] = time.time()
    # Captured output is an observation, never a replacement for sealed task
    # evidence. Preserve unknown/running/failed state and execution ownership.
    with record._lock:
        if any(record.context.get(key) != context.get(key) for key in (
                'remote_run_dir', 'task_definition_sha256', 'task_retry_count',
                'node', 'host_id', 'ssh_alias', 'remote_root', 'execution_host_configuration_sha256')):
            raise planctl.PlanError('execution advanced while diagnostics were read; observe the current task again')
        record.observation = payload
        record._persist()
    return payload
