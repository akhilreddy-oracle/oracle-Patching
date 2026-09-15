import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';

// Controller acceptance with simulated host transport and Oracle commands.
// API requests are never intercepted or fulfilled. Every mutation uses the
// visible application controls, including the ordinary managed-host routes.
const origin = 'http://127.0.0.1:18766';
const host = 'connected-fixture';
const recoveryId = 'connected-backup';
const planId = 'connected-patch';
const main = page => page.locator('#app');
const headers = role => ({ Authorization: `Bearer integration-${role}-token` });

async function signIn(page, role) {
  await page.getByLabel('API token', { exact: true }).fill(`integration-${role}-token`);
  await expect(page.locator('#rail-session')).toContainText(`Authenticated as ${role}. Roles: ${role}.`);
  await expect(page.getByRole('textbox', { name: 'Acting as', exact: true })).toHaveValue(role);
  await expect(page.getByRole('textbox', { name: 'Acting as', exact: true })).toHaveAttribute('readonly', '');
}

async function pipeline(page) {
  const response = await page.request.get(`${origin}/api/hosts/${host}/pipeline`, { headers: headers('requester') });
  expect(response.ok()).toBe(true);
  return (await response.json()).steps;
}

async function clickAndObserveTerminal(page, button, key, state) {
  // Observe the application's own polling before clicking. Native analysis and
  // evidence verification must finish before the separate UI-render deadline
  // starts. Failed/unknown runs fail immediately; this never resubmits work.
  const [response] = await Promise.all([
    page.waitForResponse(async response => {
      const url = new URL(response.url());
      if (url.origin !== origin || !/^\/api\/runs\/[a-f0-9]+$/.test(url.pathname)
          || response.request().method() !== 'GET' || response.status() !== 200) return false;
      const run = await response.json();
      return run.key === key && ['succeeded', 'failed', 'unknown'].includes(run.status);
    }, { timeout: 45_000 }),
    button.click(),
  ]);
  const run = await response.json();
  expect(run, `Terminal outcome for ${key}: ${JSON.stringify(run.error)}`).toMatchObject({ key, status: 'succeeded', result: { state } });
}

test('connected controller acceptance: prepare one fixture backup, select it, evaluate readiness, patch the same database and export verification', async ({ page }, testInfo) => {
  test.setTimeout(360_000);
  const errors = [], writes = [], external = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin === origin && url.pathname.startsWith('/api/') && request.method() !== 'GET') {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
    }
  });
  await page.route(/^https?:\/\/(?!127\.0\.0\.1:18766(?:\/|$))/, route => {
    const url = new URL(route.request().url());
    if (!['fonts.googleapis.com', 'fonts.gstatic.com'].includes(url.hostname)) external.push(url.origin);
    return route.abort('blockedbyclient');
  });
  await page.goto(`/#/hosts/${host}/recovery`);
  await signIn(page, 'requester');
  const before = await pipeline(page);
  const snapshot = before.find(step => step.step === 'discovery').evidence;
  const oracleHome = snapshot.databases[0].oracle_home;
  expect(snapshot.databases[0].db_unique_name).toBe('ORCL');
  expect(snapshot.databases[0].runtime.log_mode).toBe('NOARCHIVELOG');
  expect(before.find(step => step.step === 'readiness-evaluate').done).toBe(false);
  expect(before.find(step => step.step === 'readiness-evaluate').recovery_selection).toBeFalsy();
  const backupParent = oracleHome.replace(/\/oracle\/dbhome_1$/, '/backups');
  expect(backupParent).not.toBe(oracleHome);
  await main(page).getByLabel(/^Backup parent directory/).fill(backupParent);
  await main(page).getByText('Advanced settings — request identity and policy', { exact: true }).click();
  await main(page).getByLabel('Request ID', { exact: true }).fill(recoveryId);
  await main(page).getByRole('button', { name: 'Create live recovery request', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: recoveryId, exact: true })).toBeVisible({ timeout: 30_000 });
  await main(page).getByRole('button', { name: 'Analyze recovery', exact: true }).click();
  await expect(main(page).locator('.recovery-analysis')).toContainText('passed', { timeout: 45_000 });
  await signIn(page, 'approver');
  await main(page).getByLabel('Ticket', { exact: true }).fill('SIMULATED-CONNECTED-RECOVERY');
  await clickAndObserveTerminal(page, main(page).getByRole('button', { name: 'Approve', exact: true }),
    `recovery:${recoveryId}:approve`, 'approved');
  await expect(main(page).getByRole('heading', { name: 'Authorize', exact: true })).toBeVisible();
  await signIn(page, 'operator');
  await clickAndObserveTerminal(page, main(page).getByRole('button', { name: 'Authorize', exact: true }),
    `recovery:${recoveryId}:authorize`, 'authorized');
  await expect(main(page).getByRole('heading', { name: 'Execute', exact: true })).toBeVisible();
  await main(page).getByRole('button', { name: 'Execute', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: 'Completed', exact: true })).toBeVisible({ timeout: 90_000 });
  await page.reload();
  await expect(main(page).getByRole('heading', { name: 'Completed', exact: true })).toBeVisible();
  await main(page).getByText('Request JSON', { exact: true }).click();
  const recovery = JSON.parse(await main(page).locator('details.pipeline-result pre').innerText());
  expect(recovery).toMatchObject({ state: 'completed', host_id: host, target: { database_unique_name: 'ORCL', oracle_home: oracleHome } });
  expect(recovery.webapp_execution).toMatchObject({ terminal: true, exit_code: 0 });
  expect(recovery.result.backup_root).toBe(`${backupParent}/${recoveryId}`);
  await main(page).getByRole('link', { name: 'Validate for patch planning on this host →', exact: true }).click();
  await main(page).getByRole('button', { name: 'Validate for patch planning', exact: true }).click();
  await expect(main(page).locator('.recovery-selection')).toContainText(`Selected request: ${recoveryId}`, { timeout: 45_000 });
  await main(page).getByRole('link', { name: 'Continue to readiness evaluation →', exact: true }).click();
  await main(page).getByRole('button', { name: 'Use selected backup policy', exact: true }).click();
  await main(page).getByRole('button', { name: 'Evaluate readiness', exact: true }).click();
  await expect.poll(async () => (await pipeline(page)).find(step => step.step === 'readiness-evaluate')?.evidence?.status,
    { timeout: 45_000 }).toBe('ready_for_approval');
  const readySteps = await pipeline(page);
  const readiness = readySteps.find(step => step.step === 'readiness-evaluate');
  expect(readiness.recovery_selection).toMatchObject({ request_id: recoveryId, host_id: host, backup_root: recovery.result.backup_root });
  expect(readiness.input.recovery).toMatchObject({ require_backup: true, storage_mode: 'filesystem' });
  await testInfo.attach('connected-readiness-and-selection', { body: JSON.stringify(readiness, null, 2), contentType: 'application/json' });

  await signIn(page, 'requester');
  await main(page).locator(`.stage-rail a[href="#/hosts/${host}/plan"]`).click();
  await expect(main(page)).toContainText(`Oracle home: ${oracleHome}`);
  await main(page).getByText('Advanced settings', { exact: true }).click();
  await main(page).getByLabel('Plan ID', { exact: true }).fill(planId);
  await main(page).getByRole('button', { name: 'Create plan', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: planId, exact: true })).toBeVisible({ timeout: 30_000 });
  await signIn(page, 'approver');
  await main(page).getByLabel('Ticket', { exact: true }).fill('SIMULATED-CONNECTED-PATCH');
  await clickAndObserveTerminal(page, main(page).getByRole('button', { name: 'Approve', exact: true }),
    `plan:${planId}:approve`, 'approved');
  await expect(main(page).getByRole('heading', { name: 'Authorize', exact: true })).toBeVisible();
  await signIn(page, 'operator');
  await clickAndObserveTerminal(page, main(page).getByRole('button', { name: 'Authorize', exact: true }),
    `plan:${planId}:authorize`, 'execution_authorized');
  await expect(main(page).getByRole('heading', { name: 'Dispatch', exact: true })).toBeVisible();
  await main(page).getByRole('button', { name: 'Dispatch', exact: true }).click();
  await expect(main(page).getByRole('button', { name: 'Execute remaining tasks', exact: true })).toBeVisible({ timeout: 30_000 });
  await main(page).getByRole('button', { name: 'Execute remaining tasks', exact: true }).click();
  await expect(main(page).getByText('Apply succeeded — you can create a rollback plan from this result.', { exact: true })).toBeVisible({ timeout: 150_000 });
  await page.reload();
  await expect(main(page).getByText('Apply succeeded — you can create a rollback plan from this result.', { exact: true })).toBeVisible();
  await main(page).getByText('Plan JSON', { exact: true }).click();
  const plan = JSON.parse(await main(page).locator('details.pipeline-result pre').innerText());
  expect(plan.target.oracle_home).toBe(oracleHome);
  expect(plan.host_id).toBe(host);
  expect(plan.recovery.waived).not.toBe(true);
  expect(plan.recovery.manifest_sha256).toBe(readiness.evidence.evidence.recovery_sha256);
  const tasks = main(page).locator('section.card').filter({ has: page.getByRole('heading', { name: 'Tasks', exact: true }) });
  await expect(tasks.locator('tbody tr')).toHaveCount(5);
  for (const stage of ['precheck', 'apply', 'validate', 'datapatch', 'final_validate']) {
    await expect(tasks.getByRole('row').filter({ has: page.getByRole('cell', { name: stage, exact: true }) })).toContainText('succeeded');
  }
  await main(page).getByRole('button', { name: 'View before and after report', exact: true }).click();
  const reportView = main(page).getByRole('region', { name: 'Patch evidence report', exact: true });
  await expect(reportView).toContainText('APPLY/SUCCESS', { timeout: 45_000 });
  const downloadPromise = page.waitForEvent('download', { timeout: 60_000 });
  await reportView.getByRole('button', { name: 'Export evidence JSON', exact: true }).click();
  const download = await downloadPromise;
  const reportPath = testInfo.outputPath(download.suggestedFilename());
  await download.saveAs(reportPath);
  const report = JSON.parse(await fs.readFile(reportPath, 'utf8'));
  expect(report.completion_verified).toBe(true);
  const facts = Object.fromEntries(report.comparison.map(row => [row.metric, row.after]));
  expect(facts.sql_patch_status).toMatchObject({ status: 'verified', value: 'APPLY/SUCCESS' });
  expect(facts.listener_ready).toMatchObject({ status: 'verified', value: true });
  expect(facts.invalid_objects).toMatchObject({ status: 'verified', value: 0 });
  expect(report.rollback.approved).toBe(false);
  expect(writes.map(row => row.path)).toEqual([
    '/api/recovery', `/api/recovery/${recoveryId}/analyze`, `/api/recovery/${recoveryId}/approve`,
    `/api/recovery/${recoveryId}/authorize`, `/api/recovery/${recoveryId}/execute`,
    `/api/hosts/${host}/pipeline/recovery-collect`, `/api/hosts/${host}/pipeline/readiness-evaluate`,
    '/api/plans', `/api/plans/${planId}/approve`, `/api/plans/${planId}/authorize`,
    `/api/plans/${planId}/dispatch`, `/api/plans/${planId}/execute-remaining`,
  ]);
  expect(writes.find(row => row.path.endsWith('/readiness-evaluate')).body.policy.recovery.require_backup).toBe(true);
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
  await testInfo.attach('connected-controller-acceptance-simulated-transport', { body: JSON.stringify({
    boundary: 'Actual HTTP/controller/native tools; simulated SSH host transport and Oracle binaries. No live-lab proof.',
    recovery, readiness, plan, report,
  }, null, 2), contentType: 'application/json' });
  await page.screenshot({ path: testInfo.outputPath('connected-simulated-transport-report.png'), fullPage: true });
});
