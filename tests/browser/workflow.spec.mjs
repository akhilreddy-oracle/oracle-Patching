import { test, expect } from './fixtures.mjs';

const main = page => page.locator('#app');

test('readiness reviews focus the relevant controls without losing an unsubmitted draft', async ({ page, fixture }) => {
  fixture.steps.at(-1).evidence.gates = [
    { name: 'artifact', status: 'blocker' }, { name: 'compatibility_contract', status: 'blocker' },
    { name: 'patch_not_installed', status: 'blocker' }, { name: 'database_invalid_objects', status: 'blocker' },
  ];
  const reads = [];
  fixture.custom = async ({ url, method }) => { if (method === 'GET') reads.push(url.pathname + url.search); return false; };
  await page.goto('/#/hosts/source/readiness');
  const view = main(page), draft = view.getByLabel('Rollback precondition', { exact: true });
  await draft.fill('Keep this unsubmitted recovery condition');
  const readCount = reads.length;
  for (const [action, heading] of [['Review patch media', 'Artifact inspection'], ['Review compatibility checks', 'OPatch compatibility'],
    ['Review installed patch and selection', 'Procedure validation'], ['Review readiness controls', 'Readiness evaluation']]) {
    await view.getByRole('link', { name: action, exact: true }).click();
    await expect(view.getByRole('heading', { name: heading, exact: true })).toBeFocused();
    await expect(view.getByRole('heading', { name: heading, exact: true })).toBeInViewport();
    await expect(draft).toHaveValue('Keep this unsubmitted recovery condition');
  }
  expect(reads.length).toBe(readCount);
  expect(fixture.writes).toEqual([]);
  await expect(page).toHaveURL(/#\/hosts\/source\/readiness$/);
});

test('saved workspace target and badges follow validation without extra recovery reads or discarded policy drafts', async ({ page, fixture }) => {
  fixture.steps[0].evidence.databases.push({ db_unique_name: 'OTHER', oracle_home: '/fixture/oracle/other_home' });
  fixture.recoveries.push({ request_id: 'saved-backup', host_id: 'source', state: 'completed', evidence_mode: 'saved' });
  const recoveryReads = [];
  fixture.custom = async ({ request, url, method, send }) => {
    if (method === 'GET' && url.pathname === '/api/recovery') recoveryReads.push(url.search);
    if (method === 'POST' && url.pathname === '/api/hosts/source/pipeline/procedure-validate') {
      fixture.steps.find(step => step.step === 'procedure-validate').evidence.procedure = request.postDataJSON().procedure;
      Object.assign(fixture.steps.at(-1), { done: false, status: null, evidence: null });
      fixture.runs.validate = { status: 'succeeded' };
      await send({ run_id: 'validate' }, 202); return true;
    }
    if (method === 'POST' && url.pathname === '/api/hosts/source/pipeline/readiness-evaluate') {
      Object.assign(fixture.steps.at(-1), { done: true, status: 'ready_for_approval', evidence: { status: 'ready_for_approval' } });
      fixture.runs.ready = { status: 'succeeded' };
      await send({ run_id: 'ready' }, 202); return true;
    }
    return false;
  };
  await page.goto('/#/hosts/source/readiness');
  const view = main(page), target = view.getByRole('region', { name: 'Selected patch target', exact: true });
  const readiness = view.locator('.stage-rail').getByRole('link', { name: /^Readiness/ });
  await expect(target).toContainText('Database: ORCL');
  await expect(readiness).toContainText('blocked');
  await expect(view.locator('.stage-rail')).toContainText('Saved: completed');
  const reserve = view.getByLabel('Filesystem free space reserve (GiB)', { exact: true });
  await reserve.fill('17');
  await view.getByRole('combobox', { name: 'Database unique name', exact: true }).selectOption('OTHER');
  await view.getByRole('button', { name: 'Validate procedure', exact: true }).click();
  await expect(target).toContainText('Database: OTHER');
  await expect(target).toContainText('Patch: 39034528');
  await expect(target).toContainText('Oracle home: /fixture/oracle/other_home');
  await expect(readiness).toContainText('5/6');
  await expect(reserve).toHaveValue('17');
  await view.getByRole('button', { name: 'Evaluate readiness', exact: true }).click();
  await expect(readiness).toContainText('ready_for_approval');
  await expect(reserve).toHaveValue('17');
  expect(recoveryReads).toEqual(['?host_id=source&view=saved']);
  expect(fixture.writes.map(row => row.path)).toEqual(['/api/hosts/source/pipeline/procedure-validate', '/api/hosts/source/pipeline/readiness-evaluate']);
});

test('recovery target requirements distinguish native SPFILE analysis from unsupported or stale discovery', async ({ page, fixture }) => {
  await page.goto('/#/hosts/source/recovery');
  await expect(main(page).getByRole('button', { name: 'Create live recovery request', exact: true })).toBeEnabled();
  await expect(main(page).locator('.recovery-capability')).toContainText('Unknown: discovery does not collect SPFILE use');
  await expect(main(page).locator('.recovery-capability')).toContainText('Database is using an SPFILE');
  for (const [label, observed, required, next] of [
    ['Log mode', 'ARCHIVELOG', 'NOARCHIVELOG', 'Select a database supported by this recovery adapter'],
    ['Discovery freshness', 'Expired', 'Fresh saved discovery', 'Run Discover for this host'],
  ]) {
    fixture.recoveryCapabilities = [{ database: 'ORCL', status: 'blocked', can_create: false,
      requirements: [{ label, observed, required, status: 'blocked', next_action: next }], next_action: next }];
    await page.reload();
    await expect(main(page).getByRole('button', { name: 'Create live recovery request', exact: true })).toBeDisabled();
    const capability = main(page).locator('.recovery-capability');
    await expect(capability).toContainText('Preparation blocked');
    await expect(capability).toContainText(observed);
    await expect(capability).toContainText(required);
    await expect(capability).toContainText(next);
  }
  fixture.recoveryCapabilities = [];
  await page.reload();
  await expect(main(page).getByRole('button', { name: 'Create live recovery request', exact: true })).toBeDisabled();
  await expect(main(page).locator('.recovery-capability')).toContainText('Capability unknown');
  expect(fixture.writes).toEqual([]);
});

test('wizard reviews target, verified README and Advanced fields before accepting a maintenance window', async ({ page, fixture }, testInfo) => {
  await page.goto('/#/hosts/source/readiness');
  const view = main(page);
  await expect(view.getByText('LIVE · managed host over SSH', { exact: true })).toBeVisible();
  await expect(view.getByRole('region', { name: 'Selected patch target', exact: true }).getByText('Oracle home: /fixture/oracle/dbhome_1', { exact: true })).toBeVisible();
  await expect(view.getByRole('combobox', { name: 'Database unique name', exact: true })).toHaveValue('ORCL');
  await expect(view.getByLabel('Required OPatch', { exact: true })).toHaveValue('12.2.0.1.49');
  await expect(view.getByRole('textbox', { name: 'Staged patch path', exact: true })).toHaveCount(2);
  await expect(view.getByLabel('Mandatory prechecks', { exact: true })).not.toBeVisible();
  await view.getByText('Advanced settings — procedure contract', { exact: true }).click();
  await expect(view.getByLabel('Mandatory prechecks', { exact: true })).toBeVisible();
  await expect(view.locator('.readiness-finding')).toContainText('ActualUnknown — evidence not supplied');
  await expect(view.locator('.readiness-finding')).toContainText('RequiredBackup age ≤ 1440 minutes');
  await page.screenshot({ path: testInfo.outputPath('readiness-wizard.png'), fullPage: true });
  await view.locator('.stage-rail').getByRole('link', { name: 'Plan', exact: false }).click();
  await expect(view.getByText('README bound to validated procedure', { exact: true })).toBeVisible();
  await expect(view.getByLabel('Plan ID', { exact: true })).not.toBeVisible();
  await view.getByLabel('Window start (UTC)', { exact: true }).fill('2030-01-01T10:00:00');
  await view.getByRole('button', { name: 'Create plan', exact: true }).click();
  await expect(view.getByText(/Use valid UTC calendar timestamps/)).toBeVisible();
  await view.getByLabel('Window start (UTC)', { exact: true }).fill('2030-02-30T10:00:00Z');
  await view.getByLabel('Window end (UTC)', { exact: true }).fill('2030-03-03T10:00:00Z');
  await view.getByRole('button', { name: 'Create plan', exact: true }).click();
  await expect(view.getByText(/Use valid UTC calendar timestamps/)).toBeVisible();
  expect(fixture.writes).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('plan-review.png'), fullPage: true });
});

test('editing a plan ID during creation cannot redirect away from the submitted plan', async ({ page, fixture }) => {
  let releaseRun;
  fixture.custom = async ({ url, method, send }) => {
    if (method === 'POST' && url.pathname === '/api/plans') {
      fixture.plans.push({ plan_id: 'submitted-plan', state: 'awaiting_approval', requester: 'fixture-operator' });
      await send({ run_id: 'create-plan' }, 202); return true;
    }
    if (method === 'GET' && url.pathname === '/api/runs/create-plan') {
      await new Promise(resolve => { releaseRun = resolve; });
      await send({ status: 'succeeded' }); return true;
    }
    if (method === 'GET' && url.pathname === '/api/plans/submitted-plan/execution') { await send({ runs: [], tasks: [] }); return true; }
    if (method === 'GET' && url.pathname === '/api/plans/submitted-plan/tasks') { await send({ tasks: [] }); return true; }
    if (method === 'GET' && url.pathname === '/api/itsm/tickets') { await send({ tickets: [] }); return true; }
    return false;
  };
  await page.goto('/#/hosts/source/plan');
  await main(page).getByText('Advanced settings', { exact: true }).click();
  await main(page).getByLabel('Plan ID', { exact: true }).fill('submitted-plan');
  await main(page).getByRole('button', { name: 'Create plan', exact: true }).click();
  await expect.poll(() => Boolean(releaseRun)).toBe(true);
  await main(page).getByLabel('Plan ID', { exact: true }).fill('edited-after-submit');
  releaseRun();
  await expect(page).toHaveURL(/#\/plans\/submitted-plan$/);
  await expect(main(page).getByRole('heading', { name: 'submitted-plan', exact: true })).toBeVisible();
  expect(fixture.writes).toHaveLength(1);
  expect(fixture.writes[0].body.plan_id).toBe('submitted-plan');
});

test('discovery controls explain permissions and refresh them on navigation', async ({ page, fixture }) => {
  fixture.session = { mode: 'principal', actor: 'fixture-requester', roles: ['requester'], rbac_enabled: true,
    permissions: { live_discovery: false } };
  await page.goto('/#/estate');
  await expect(main(page).getByRole('button', { name: 'Refresh live SSH', exact: true })).toBeDisabled();
  await expect(main(page).getByText(/Your account cannot run live discovery/)).toBeVisible();
  await page.locator('#rail-hosts').getByRole('link', { name: /Source lab fixture/ }).click();
  await expect(main(page).getByRole('button', { name: 'Run live discovery', exact: true })).toBeDisabled();
  await expect(main(page).getByText(/Your account cannot run live discovery/)).toBeVisible();
  fixture.session = { mode: 'principal', actor: 'fixture-operator', roles: ['operator'], rbac_enabled: true,
    permissions: { live_discovery: true } };
  await page.reload();
  await expect(main(page).getByRole('button', { name: 'Run live discovery', exact: true })).toBeEnabled();
  await page.goto('/#/estate');
  await expect(main(page).getByRole('button', { name: 'Refresh live SSH', exact: true })).toBeEnabled();
  delete fixture.session.permissions;
  await page.reload();
  await expect(main(page).getByRole('button', { name: 'Refresh live SSH', exact: true })).toBeDisabled();
  await expect(main(page).getByText(/Live discovery permissions are unavailable/)).toBeVisible();
  expect(fixture.writes).toEqual([]);
});

test('a discovery run with an unreadable refreshed snapshot never displays green success', async ({ page, fixture }) => {
  let refreshed = false;
  fixture.custom = async ({ url, method, send }) => {
    if (method === 'POST' && url.pathname === '/api/hosts/source/pipeline/discovery') {
      refreshed = true;
      fixture.runs.discover = { run_id: 'discover', status: 'succeeded' };
      await send({ run_id: 'discover' }, 202); return true;
    }
    if (method === 'GET' && url.pathname === '/api/hosts/source/pipeline' && refreshed) {
      await send({ message: 'Snapshot could not be read' }, 503); return true;
    }
    return false;
  };
  await page.goto('/#/hosts/source/discover');
  const status = main(page).locator('#discover-status');
  await expect(status).toHaveText('complete');
  await main(page).getByRole('button', { name: 'Run live discovery', exact: true }).click();
  await expect(status).toHaveText('unavailable');
  await expect(status).toHaveClass(/is-bad/);
  await expect(main(page).getByText(/Snapshot could not be read/)).toBeVisible();
  await expect(main(page).getByRole('button', { name: 'Run live discovery', exact: true })).toBeEnabled();
  expect(fixture.writes.map(row => row.path)).toEqual(['/api/hosts/source/pipeline/discovery']);
});

test('leaving estate during an accepted discovery keeps the new host selected without obsolete reads', async ({ page, fixture }) => {
  let releaseDiscovery;
  const reads = [];
  fixture.custom = async ({ url, method, send }) => {
    if (method === 'GET') reads.push(url.pathname);
    if (method === 'POST' && url.pathname === '/api/hosts/source/pipeline/discovery') {
      await new Promise(resolve => { releaseDiscovery = resolve; });
      await send({ run_id: 'late-estate-discovery' }, 202); return true;
    }
    return false;
  };
  await page.goto('/#/estate');
  await main(page).getByRole('button', { name: 'Refresh live SSH', exact: true }).click();
  await expect.poll(() => Boolean(releaseDiscovery)).toBe(true);
  await page.locator('#rail-hosts').getByRole('link', { name: /Source lab fixture/ }).click();
  await expect(main(page).locator('#discover-status')).toHaveText('complete');
  await expect(main(page).locator('.route-view')).toHaveAttribute('aria-busy', 'false');
  const count = reads.length;
  const finished = page.waitForEvent('requestfinished', request => request.method() === 'POST'
    && request.url().endsWith('/api/hosts/source/pipeline/discovery'));
  releaseDiscovery(); await finished;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect(reads.length).toBe(count);
  await expect(page.locator('#rail-hosts').getByRole('link', { name: /Source lab fixture/ })).toHaveAttribute('aria-current', 'page');
  expect(fixture.writes.map(row => row.path)).toEqual(['/api/hosts/source/pipeline/discovery']);
});

test('reconnecting to an active backup analysis does not offer another analysis launch', async ({ page, fixture }) => {
  fixture.recoveries.push({ request_id: 'active-analysis', state: 'awaiting_approval', mode: 'live',
    analysis: { status: 'passed' }, latest_run: { run_id: 'existing-analysis', status: 'running' } });
  await page.goto('/#/recovery/active-analysis');
  await expect(main(page).getByRole('button', { name: 'Refresh analysis', exact: true })).toBeDisabled();
  await expect(main(page).getByText('Existing operation must finish or be reconciled', { exact: true })).toBeVisible();
  await expect(main(page).getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
  expect(fixture.writes).toEqual([]);
});

test('fleet filters preserve unknown and stale evidence instead of displaying compliant green status', async ({ page, fixture }, testInfo) => {
  await page.goto('/#/estate');
  const fleet = main(page).locator('.fleet-dashboard');
  await expect(fleet.getByRole('row')).toHaveCount(4);
  const stale = fleet.getByRole('row').filter({ hasText: 'OLD' });
  await expect(stale).toContainText('unknown');
  await expect(stale).not.toContainText('ready_for_approval');
  await fleet.getByLabel('Environment', { exact: true }).selectOption('unknown');
  await expect(fleet.getByRole('row')).toHaveCount(2);
  await expect(fleet.getByRole('row').filter({ hasText: 'UNKNOWN' })).toContainText('Observation time unknown');
  await fleet.getByLabel('Environment', { exact: true }).selectOption('all');
  await fleet.getByLabel('Backup freshness', { exact: true }).selectOption('missing');
  await expect(fleet.getByRole('row')).toHaveCount(2);
  await expect(fleet.getByRole('link', { name: 'Review backup', exact: true })).toHaveAttribute('href', '#/hosts/source/recovery');
  expect(fixture.writes).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('fleet-compliance.png'), fullPage: true });
});

test('backup preparation keeps analysis and approval gates, then confirms selected evidence for readiness', async ({ page, fixture }, testInfo) => {
  const request = { request_id: 'backup-browser', host_id: 'source', mode: 'live', state: 'awaiting_approval', requester: 'fixture-requester', target: { database_unique_name: 'ORCL', oracle_home: '/fixture/oracle/dbhome_1' }, analysis: { status: 'blocked', reason: 'Insufficient filesystem capacity' } };
  fixture.recoveries.push(request);
  await page.goto('/#/recovery/backup-browser');
  await expect(main(page).getByRole('button', { name: 'Approve', exact: true })).toBeDisabled();
  await expect(main(page).getByText('Insufficient filesystem capacity', { exact: true })).toBeVisible();
  request.state = 'completed';
  request.result = { recovery_evidence: { path: '/fixture/recovery-evidence.json' } };
  fixture.custom = async ({ url, method, send }) => {
    if (method === 'POST' && url.pathname.endsWith('/recovery-collect')) {
      fixture.steps.at(-1).recovery_selection = { request_id: request.request_id, host_id: 'source', policy: fixture.policy };
      fixture.runs.collect = { run_id: 'collect', status: 'succeeded', result: { status: 'passed' } };
      await send({ run_id: 'collect' }, 202); return true;
    }
    return false;
  };
  await page.goto('/#/hosts/source/recovery');
  await main(page).getByRole('button', { name: 'Validate for patch planning', exact: true }).click();
  await expect(main(page).locator('.recovery-selection')).toContainText('Selected request: backup-browser');
  await main(page).getByRole('link', { name: 'Continue to readiness evaluation →', exact: true }).click();
  await expect(main(page).getByRole('button', { name: 'Use selected backup policy', exact: true })).toBeVisible();
  expect(fixture.writes.map(row => row.path)).toEqual(['/api/hosts/source/pipeline/recovery-collect']);
  await page.screenshot({ path: testInfo.outputPath('selected-backup.png'), fullPage: true });
});

test('unknown backup outcome survives reconnect and permits record inspection without relaunch', async ({ page, fixture }) => {
  const request = { request_id: 'backup-unknown', host_id: 'source', mode: 'live', state: 'authorized', latest_run: { run_id: 'run-disconnect', status: 'unknown', error: { message: 'Connection lost after launch' }, context: { request_id: 'backup-unknown' } } };
  fixture.recoveries.push(request);
  fixture.custom = async ({ url, method, send }) => {
    if (method === 'POST' && url.pathname === '/api/runs/run-disconnect/reconcile') {
      request.latest_run.status = 'failed'; request.state = 'recovery_required';
      await send(request.latest_run); return true;
    } return false;
  };
  await page.goto('/#/recovery/backup-unknown');
  await page.reload();
  await expect(main(page).getByText('Connection lost after launch', { exact: true })).toBeVisible();
  await expect(main(page).getByRole('button', { name: 'Execute', exact: true })).toHaveCount(0);
  await main(page).getByRole('button', { name: 'Inspect and reconcile', exact: true }).click();
  expect(fixture.writes.map(row => row.path)).toEqual(['/api/runs/run-disconnect/reconcile']);
});

test('approval inbox separates review from execution and flags self-requested work', async ({ page, fixture }, testInfo) => {
  fixture.custom = async ({ url, method, send }) => {
    if (method === 'GET' && url.pathname === '/api/approvals') {
      await send({ actor: 'fixture-operator', items: [{ id: 'review-1', kind: 'plan', state: 'awaiting_approval', requester: 'fixture-operator', self_requested: true, host_id: 'source', target: { database_unique_name: 'ORCL' } }] }); return true;
    } return false;
  };
  await page.goto('/#/approvals');
  await expect(main(page).getByRole('heading', { name: 'Approval inbox', exact: true })).toBeVisible();
  await expect(main(page).getByText(/An independent approver is required/)).toBeVisible();
  await expect(main(page).getByRole('link', { name: 'Review request', exact: true })).toHaveAttribute('href', '#/plans/review-1');
  expect(fixture.writes).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('approval-inbox.png'), fullPage: true });
});

test('execution dashboard retains unknown outcome, distinguishes heartbeat, and exports only verified report facts', async ({ page, fixture }, testInfo) => {
  const now = Math.floor(Date.now() / 1000);
  const run = { run_id: 'patch-run', status: 'unknown', can_observe: true, elapsed_seconds: 120, controller_poll: { observed_at: now, state: 'disconnected' }, observation: { remote_clock: now, received_at: now, worker_heartbeat: { available: true, last_renewed_at: now - 30, task_status: 'pending' }, logs: { 'stdout.log': { text: 'Fixture native datapatch validation output', truncated: false } } }, context: { plan_id: 'patch-browser', task_id: '005-final-validate-local' }, error: { message: 'Native terminal result remains unverified' } };
  const tasks = [{ task_id: '002-apply-source', stage: 'apply', node: 'source', status: 'succeeded', claimed_at_epoch: now - 1000, completed_at_epoch: now - 800, evidence_verified: true }, { task_id: '004-datapatch-local', stage: 'datapatch', node: 'local', status: 'succeeded', claimed_at_epoch: now - 500, completed_at_epoch: now - 300, evidence_verified: true }, { task_id: '005-final-validate-local', stage: 'final_validate', node: 'local', status: 'pending', evidence_verified: false }];
  const dashboard = { state: 'paused', tasks, runs: [run], timeline: [{ at: now, event: 'unknown', message: 'Connection lost; reconcile the existing run.', task_id: '005-final-validate-local' }], guidance: 'Inspect and reconcile the interrupted run. Applying the patch again is blocked.' };
  const report = { plan_id: 'patch-browser', after_scope: 'Partial task evidence', interpretation: 'Final validation is incomplete; this is not a completed patch report.', comparison: [
    { label: 'SQL patch status', before: { status: 'verified', value: 'Absent' }, after: { status: 'verified', value: 'APPLY SUCCESS', source: { label: 'Datapatch task evidence' } } },
    { label: 'Listener health', before: { status: 'verified', value: 'READY' }, after: { status: 'unknown', value: 'unverified text must not appear' } },
  ], rollback: { status: 'unknown', reason: 'Native eligibility has not been verified.' }, gaps: [{ source: 'final validation', reason: 'Pending task' }] };
  fixture.plans.push({ plan_id: 'patch-browser', host_id: 'source', state: 'paused', intent: 'patch_apply', patch_id: '39034528', requester: 'fixture-requester', unresolved_run: run, maintenance_window: { start: '2030-01-01T10:00:00Z', end: '2030-01-01T14:00:00Z' } });
  fixture.custom = async ({ url, method, send }) => {
    if (method === 'GET' && url.pathname === '/api/plans/patch-browser/tasks') { await send({ tasks }); return true; }
    if (method === 'GET' && url.pathname === '/api/plans/patch-browser/execution') { await send(dashboard); return true; }
    if (method === 'GET' && url.pathname === '/api/plans/patch-browser/report') { await send(report); return true; }
    if (method === 'POST' && url.pathname === '/api/plans/patch-browser/execution-observe') {
      fixture.runs.observe = { run_id: 'observe', status: 'succeeded', result: { status: 'observed' } };
      await send({ run_id: 'observe' }, 202); return true;
    } return false;
  };
  await page.goto('/#/plans/patch-browser');
  const view = main(page);
  await expect(view.getByRole('heading', { name: 'Execution dashboard', exact: true })).toBeVisible();
  await expect(view.getByRole('list', { name: 'Persistent task timeline', exact: true })).toContainText('final_validate');
  await expect(view.getByRole('button', { name: 'Execute remaining tasks', exact: true })).toHaveCount(0);
  await view.getByText('Native logs · bounded and redacted', { exact: true }).click();
  await expect(view.getByText('Fixture native datapatch validation output', { exact: true })).toBeVisible();
  await view.getByRole('button', { name: 'Refresh native logs', exact: true }).click();
  await expect(view.getByText(/Native lease last renewed/)).toBeVisible();
  await view.getByRole('button', { name: 'View before and after report', exact: true }).click();
  await expect(view.getByRole('row').filter({ hasText: 'Listener health' })).toContainText('Unknown');
  await expect(view.getByText('unverified text must not appear', { exact: true })).toHaveCount(0);
  const downloadPromise = page.waitForEvent('download');
  await view.getByRole('button', { name: 'Export evidence JSON', exact: true }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('patch-browser-evidence.json');
  await download.saveAs(testInfo.outputPath('patch-browser-evidence.json'));
  expect(fixture.writes.map(row => row.path)).toEqual(['/api/plans/patch-browser/execution-observe']);
  await page.screenshot({ path: testInfo.outputPath('execution-dashboard.png'), fullPage: true });
});
