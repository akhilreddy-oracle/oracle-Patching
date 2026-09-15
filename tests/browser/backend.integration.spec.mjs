import { test as base, expect } from '@playwright/test';
import fs from 'node:fs/promises';

const origin = 'http://127.0.0.1:18766';
const main = page => page.locator('#app');

const test = base.extend({
  audit: [async ({ page }, use) => {
    const audit = { writes: [], pageErrors: [], external: [] };
    page.on('pageerror', error => audit.pageErrors.push(error.message));
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.origin === origin && url.pathname.startsWith('/api/') && request.method() !== 'GET') {
        audit.writes.push({ path: url.pathname, body: request.postDataJSON() });
      }
    });
    await page.route(/^https?:\/\/(?!127\.0\.0\.1:18766(?:\/|$))/, async route => {
      const url = new URL(route.request().url());
      // The pattern excludes the controller entirely: its API reads/writes are
      // never intercepted. Only browser egress (including fonts) is denied.
      if (!['fonts.googleapis.com', 'fonts.gstatic.com'].includes(url.hostname)) audit.external.push(url.origin);
      return route.abort('blockedbyclient');
    });
    await use(audit);
    expect(audit.pageErrors).toEqual([]);
    expect(audit.external).toEqual([]);
  }, { auto: true }],
});

async function signIn(page, role) {
  await page.getByLabel('API token', { exact: true }).fill(`integration-${role}-token`);
  await expect(page.locator('#rail-session')).toContainText(`Authenticated as ${role}. Roles: ${role}.`);
  await expect(page.getByRole('textbox', { name: 'Acting as', exact: true })).toHaveValue(role);
  await expect(page.getByRole('textbox', { name: 'Acting as', exact: true })).toHaveAttribute('readonly', '');
}

test('actual HTTP authentication denies anonymous access and an operator creating requester work', async ({ page, audit }) => {
  await page.goto('/#/recovery/new');
  await expect(main(page)).toContainText('Authentication required.');
  await expect(main(page).getByRole('button', { name: 'Build fixture and create request', exact: true })).toHaveCount(0);
  await page.getByLabel('API token', { exact: true }).fill('not-an-integration-principal');
  await expect(main(page)).toContainText('Missing or invalid principal credential');
  await signIn(page, 'operator');
  await main(page).getByLabel('Request ID', { exact: true }).fill('forbidden-operator-request');
  const rejected = page.waitForResponse(response => response.url() === `${origin}/api/recovery/testmode-demo` && response.request().method() === 'POST');
  await main(page).getByRole('button', { name: 'Build fixture and create request', exact: true }).click();
  expect((await rejected).status()).toBe(403);
  await expect(main(page)).toContainText('lacks role for action create');
  expect(audit.writes).toEqual([{ path: '/api/recovery/testmode-demo', body: { request_id: 'forbidden-operator-request', requester: 'operator' } }]);
});

test('actual HTTP recovery: separate authenticated roles prepare and validate a fixture backup, retained after reload', async ({ page, audit }, testInfo) => {
  test.setTimeout(180_000);
  await page.goto('/#/recovery/new');
  await signIn(page, 'requester');
  await main(page).getByLabel('Request ID', { exact: true }).fill('browser-recovery');
  await main(page).getByRole('button', { name: 'Build fixture and create request', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: 'browser-recovery', exact: true })).toBeVisible();
  await expect(main(page).getByRole('button', { name: 'Approve', exact: true })).toBeDisabled();
  await main(page).getByRole('button', { name: 'Analyze recovery', exact: true }).click();
  await expect(main(page).locator('.recovery-analysis')).toContainText('passed', { timeout: 45_000 });
  await expect(main(page).getByRole('button', { name: 'Approve', exact: true })).toBeEnabled();
  await main(page).getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(main(page).getByText('The requester cannot approve their own recovery preparation.', { exact: true })).toBeVisible();
  expect(audit.writes.filter(row => row.path.endsWith('/approve'))).toHaveLength(0);

  await signIn(page, 'approver');
  // A token switch refreshes the page: analysis must survive in saved controller metadata.
  await expect(main(page).locator('.recovery-analysis')).toContainText('passed');
  await main(page).getByLabel('Ticket', { exact: true }).fill('FIXTURE-BROWSER-RECOVERY');
  await main(page).getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: 'Authorize', exact: true })).toBeVisible();
  await signIn(page, 'operator');
  await main(page).getByRole('button', { name: 'Authorize', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: 'Execute', exact: true })).toBeVisible();
  await main(page).getByRole('button', { name: 'Execute', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: 'Completed', exact: true })).toBeVisible({ timeout: 90_000 });
  await page.reload();
  await expect(main(page).getByRole('heading', { name: 'Completed', exact: true })).toBeVisible();
  await expect(main(page).getByText('This evidence belongs to the TEST_MODE fixture.', { exact: true })).toBeVisible();
  await main(page).getByText('Request JSON', { exact: true }).click();
  const request = JSON.parse(await main(page).locator('details.pipeline-result pre').innerText());
  expect(request.state).toBe('completed');
  expect(request.mode).toBe('test_mode');
  expect(request.requester).toBe('requester');
  expect(request.result.recovery_evidence.path).toContain('recovery-evidence.json');
  expect(audit.writes.map(row => row.path)).toEqual([
    '/api/recovery/testmode-demo', '/api/recovery/browser-recovery/analyze',
    '/api/recovery/browser-recovery/approve', '/api/recovery/browser-recovery/authorize',
    '/api/recovery/browser-recovery/execute',
  ]);
  expect(audit.writes.at(-3).body.actor).toBe('approver');
  expect(audit.writes.at(-1).body.actor).toBe('operator');
  await testInfo.attach('recovery-completion', { body: JSON.stringify(request, null, 2), contentType: 'application/json' });
  await page.screenshot({ path: testInfo.outputPath('real-backend-recovery.png'), fullPage: true });
});

test('actual HTTP patch: create, independent approval, five native fixture stages and verified downloaded report', async ({ page, audit }, testInfo) => {
  test.setTimeout(240_000);
  await page.goto('/#/plans/demo-new');
  await signIn(page, 'requester');
  await main(page).getByLabel('Plan ID', { exact: true }).fill('browser-patch');
  await main(page).getByRole('button', { name: 'Build fixture and create plan', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: 'browser-patch', exact: true })).toBeVisible({ timeout: 45_000 });
  await signIn(page, 'approver');
  await main(page).getByLabel('Ticket', { exact: true }).fill('FIXTURE-BROWSER-PATCH');
  await main(page).getByRole('button', { name: 'Approve', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: 'Authorize', exact: true })).toBeVisible();
  await signIn(page, 'operator');
  await main(page).getByRole('button', { name: 'Authorize', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: 'Dispatch', exact: true })).toBeVisible();
  await main(page).getByRole('button', { name: 'Dispatch', exact: true }).click();
  await expect(main(page).getByRole('button', { name: 'Execute remaining tasks', exact: true })).toBeVisible({ timeout: 30_000 });
  await main(page).getByRole('button', { name: 'Execute remaining tasks', exact: true }).click();
  await expect(main(page).getByText('Apply succeeded — you can create a rollback plan from this result.', { exact: true })).toBeVisible({ timeout: 150_000 });
  await page.reload();
  await expect(main(page).getByText('Apply succeeded — you can create a rollback plan from this result.', { exact: true })).toBeVisible();
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
  expect(report.rollback.status).toBe('requires_native_validation');
  expect(report.rollback.approved).toBe(false);
  const facts = Object.fromEntries(report.comparison.map(row => [row.metric, row.after]));
  expect(facts.sql_patch_status).toMatchObject({ status: 'verified', value: 'APPLY/SUCCESS' });
  expect(facts.listener_ready).toMatchObject({ status: 'verified', value: true });
  expect(facts.invalid_objects).toMatchObject({ status: 'verified', value: 0 });
  expect(audit.writes.map(row => row.path)).toEqual([
    '/api/plans/testmode-demo', '/api/plans/browser-patch/approve', '/api/plans/browser-patch/authorize',
    '/api/plans/browser-patch/dispatch', '/api/plans/browser-patch/execute-remaining',
  ]);
  await testInfo.attach('verified-fixture-report', { path: reportPath, contentType: 'application/json' });
  await page.screenshot({ path: testInfo.outputPath('real-backend-patch-report.png'), fullPage: true });
});
