import { test as base, expect } from '@playwright/test';

export const origin = 'http://127.0.0.1:18765';
export function fixtureState() {
  const now = new Date().toISOString();
  const artifact = { sha256: 'a'.repeat(64), status: 'ready_for_catalog', patch_ids: ['39034528'], platforms: [{ id: '226' }], path: '/fixture/stage/39034528', readme_files: [{ path: 'README.html', sha256: 'b'.repeat(64) }] };
  const policy = { schema_version: '1.0', maximum_snapshot_age_seconds: 1800, require_xml_inventory: true, recovery: { require_backup: true, storage_mode: 'filesystem', capacity_basis: 'allocated', max_backup_age_minutes: 1440, minimum_fra_free_bytes: 0, minimum_filesystem_free_bytes: 10737418240, require_guaranteed_restore_point: false }, database: { require_primary_read_write: true, maximum_invalid_objects: 0 } };
  const procedure = { schema_version: '1.0', artifact_sha256: artifact.sha256, patch_id: '39034528', target: { family: 'database', method: 'opatch', topology: 'single_instance', platform_id: '226', database_unique_name: 'ORCL' }, execution: { adapter: 'database_single_instance_opatch', operations: ['database_shutdown', 'database_opatch_apply', 'database_startup', 'database_datapatch'] }, required_opatch_version: '12.2.0.1.49', oracle_references: [{ kind: 'patch_readme', identifier: 'README.html', sha256: 'b'.repeat(64) }], mandatory_prechecks: ['artifact_integrity', 'backup_or_restore'], mandatory_postchecks: ['binary_inventory', 'service_health'], rollback: { mode: 'opatch_rollback', precondition: 'Verify a supported backup and the matching patch inventory.' } };
  const snapshot = { host: { name: 'source' }, collected_at: now, cluster: { status: 'unavailable' }, databases: [{ db_unique_name: 'ORCL', oracle_home: '/fixture/oracle/dbhome_1', runtime: { status: 'complete', database_role: 'PRIMARY', instance_state: 'OPEN', open_mode: 'READ WRITE', log_mode: 'NOARCHIVELOG', backup_age_minutes: -1, invalid_objects: 0 } }], oracle_homes: [{ path: '/fixture/oracle/dbhome_1', owner: 'oracle', version: '19.3', opatch_version: '12.2.0.1.51', patches: [] }] };
  const steps = [
    { step: 'discovery', done: true, status: 'complete', evidence: snapshot },
    { step: 'reconcile', done: true, status: 'consistent' },
    { step: 'artifact-inspect', done: true, status: 'ready_for_catalog', evidence: { artifact } },
    { step: 'procedure-validate', done: true, status: 'ready_for_planning', evidence: { status: 'ready_for_planning', procedure } },
    { step: 'compatibility-collect', done: true, status: 'passed' },
    { step: 'compatibility-reconcile', done: true, status: 'passed' },
    { step: 'readiness-evaluate', done: true, status: 'blocked', input: policy, evidence: { status: 'blocked', patch_id: '39034528', gates: [{ name: 'recovery_backup', status: 'blocker', detail: 'source database ORCL has no backup within policy age.' }], evaluated_at: now } },
  ];
  return {
    artifact, procedure, policy, steps, writes: [], unexpected: [], pageErrors: [],
    session: { mode: 'lab', rbac_enabled: false, actor: 'fixture-operator', roles: [], permissions: { live_discovery: true } },
    estate: { hosts: [{ id: 'source', label: 'Source lab fixture', status: 'ok', cluster_status: 'not_applicable', databases: [{ db_unique_name: 'ORCL', instance_state: 'OPEN' }], active_version: '19.3', node_count: 1, oracle_home_count: 1 }] },
    fleet: { generated_at: now, databases: [
      { host_id: 'source', database: 'ORCL', oracle_home: '/fixture/oracle/dbhome_1', environment: 'lab', oracle_version: '19.3', patch_baseline: '19.3', desired_patch_baseline: '39034528', baseline_status: 'behind', backup_status: 'missing', readiness: 'blocked', evidence_status: 'fresh', evidence_at: now, blockers: 1 },
      { host_id: 'stale', database: 'OLD', environment: 'production', oracle_version: '19.31', patch_baseline: '39034528', baseline_status: 'compliant', backup_status: 'stale', readiness: 'ready_for_approval', evidence_status: 'stale', evidence_at: '2020-01-01T00:00:00Z' },
      { host_id: 'unobserved', database: 'UNKNOWN', environment: null, oracle_version: null, backup_status: 'unknown', readiness: 'unknown', evidence_status: 'unknown' },
    ] },
    recoveryCapabilities: [{ database: 'ORCL', status: 'needs_native_analysis', can_create: true,
      supported_adapter: 'standalone_primary_noarchivelog_spfile', blockers: [],
      requirements: [{ id: 'spfile', label: 'SPFILE', observed: 'Unknown: discovery does not collect SPFILE use',
        required: 'Database is using an SPFILE', status: 'unknown', stage: 'native_analysis',
        next_action: 'Create the request and run Analyze recovery; approval remains blocked until the native probe passes' }],
      next_action: 'Create a request for native analysis; this is not approval to execute' }],
    recoveries: [], plans: [], runs: {}, custom: null,
  };
}

export const test = base.extend({
  seedLabCredentials: [true, { option: true }],
  fixture: async ({ page, seedLabCredentials }, use) => {
    const state = fixtureState();
    if (seedLabCredentials) {
      await page.addInitScript(() => {
        localStorage.setItem('opu-webapp-token', 'browser-fixture-token');
        localStorage.setItem('opu-webapp-actor', 'fixture-operator');
      });
    }
    page.on('pageerror', error => state.pageErrors.push(error.message));
    await page.route('**/*', async (route) => {
      const request = route.request(); const url = new URL(request.url());
      // There is no route fallback to a live service. External font requests
      // receive empty fixture CSS; all other external destinations are denied.
      if (url.origin !== origin) {
        if (['fonts.googleapis.com', 'fonts.gstatic.com'].includes(url.hostname)) return route.fulfill({ status: 200, contentType: 'text/css', body: '' });
        state.unexpected.push(`external ${request.method()} ${url.origin}`); return route.abort('blockedbyclient');
      }
      if (!url.pathname.startsWith('/api/')) return route.continue();
      const method = request.method();
      if (method !== 'GET') state.writes.push({ path: url.pathname, method, body: request.postDataJSON() });
      const send = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (state.custom && await state.custom({ request, url, method, send, route })) return;
      if (method === 'GET') {
        if (url.pathname === '/api/auth/config') return send({ configured: false });
        if (url.pathname === '/api/session') return send(state.session);
        if (url.pathname === '/api/estate') return send(state.estate);
        if (url.pathname === '/api/fleet') return send(state.fleet);
        if (url.pathname === '/api/hosts/source/pipeline') return send({ steps: state.steps });
        if (url.pathname === '/api/recovery') return send({ requests: state.recoveries, live_available: true, live_reason: 'Fixture capability response only', supported_adapter: 'standalone_primary_noarchivelog_spfile', target_capability_context: { host_id: 'source' }, target_capabilities: state.recoveryCapabilities });
        if (url.pathname === '/api/plans') return send({ plans: state.plans });
        const recovery = state.recoveries.find(row => url.pathname === `/api/recovery/${row.request_id}`);
        if (recovery) return send(recovery);
        const plan = state.plans.find(row => url.pathname === `/api/plans/${row.plan_id}`);
        if (plan) return send(plan);
        if (url.pathname.startsWith('/api/runs/')) {
          const run = state.runs[url.pathname.split('/').at(-1)]; if (run) return send(run);
        }
      }
      state.unexpected.push(`${method} ${url.pathname}`);
      return send({ message: 'Unmocked API route; live access is disabled' }, 501);
    });
    await use(state);
    expect(state.unexpected, 'Every API call must resolve to a fixture; live requests are forbidden').toEqual([]);
    expect(state.pageErrors, 'Browser must not contain uncaught application errors').toEqual([]);
  },
});
export { expect };
