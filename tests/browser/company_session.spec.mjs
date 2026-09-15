import { test, expect } from './fixtures.mjs';

// These tests exercise the real browser UI with mocked authentication/API
// responses. They do not verify a real identity-provider login or live Oracle.
test.use({ seedLabCredentials: false });

function companySession(expiresAt = Math.floor(Date.now() / 1000) + 300) {
  return {
    mode: 'company', rbac_enabled: true, actor: 'sso-fixture-operator',
    display_name: 'Company fixture operator', roles: ['operator'],
    csrf_token: 'fixture-company-csrf', expires_at: expiresAt,
  };
}

function interruptedRecovery(fixture) {
  const request = {
    request_id: 'company-session-recovery', host_id: 'source', mode: 'live',
    state: 'authorized', latest_run: {
      run_id: 'company-session-run', status: 'unknown',
      error: { message: 'Fixture connection lost after launch' },
      context: { request_id: 'company-session-recovery' },
    },
  };
  fixture.recoveries.push(request);
  return request;
}

test('fresh company session without lab token can inspect an existing run with bound actor and CSRF', async ({ page, fixture }) => {
  fixture.session = companySession();
  const recovery = interruptedRecovery(fixture);
  let observedHeaders;
  fixture.custom = async ({ request, url, method, send }) => {
    if (method === 'GET' && url.pathname === '/api/auth/config') {
      await send({ configured: true, login_url: '/api/auth/login', label: 'Company sign in' });
      return true;
    }
    if (method === 'POST' && url.pathname === '/api/runs/company-session-run/reconcile') {
      observedHeaders = request.headers();
      recovery.latest_run.status = 'failed';
      recovery.state = 'recovery_required';
      await send(recovery.latest_run);
      return true;
    }
    return false;
  };
  await page.goto('/#/recovery/company-session-recovery');
  await expect(page.getByLabel('API token', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('Acting as', { exact: true })).toHaveValue('sso-fixture-operator');
  await expect(page.getByLabel('Acting as', { exact: true })).toHaveAttribute('readonly', '');
  const card = page.locator('#app').getByRole('alert').filter({ hasText: 'Execution outcome needs reconciliation' });
  await expect(card.getByLabel('Actor', { exact: true })).toHaveValue('sso-fixture-operator');
  await expect(card.getByLabel('Actor', { exact: true })).toHaveAttribute('readonly', '');
  await card.getByRole('button', { name: 'Inspect and reconcile', exact: true }).click();
  await expect.poll(() => fixture.writes.length, { timeout: 3000 }).toBe(1);
  expect(fixture.writes).toEqual([{
    path: '/api/runs/company-session-run/reconcile', method: 'POST',
    body: { actor: 'sso-fixture-operator' },
  }]);
  expect(observedHeaders['x-opu-actor']).toBe('sso-fixture-operator');
  expect(observedHeaders['x-csrf-token']).toBe('fixture-company-csrf');
  expect(observedHeaders.authorization).toBeUndefined();
  await expect(page.locator('#app').getByText('Run reconciled', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('opu-webapp-token'))).toBeNull();
});

test('expired company session cannot submit record inspection without renewing authentication', async ({ page, fixture }) => {
  fixture.session = companySession(Math.floor(Date.now() / 1000) - 1);
  interruptedRecovery(fixture);
  await page.goto('/#/recovery/company-session-recovery');
  const view = page.locator('#app');
  await view.getByRole('button', { name: 'Inspect and reconcile', exact: true }).click();
  await expect(view.getByText('Company session expired or unavailable. Sign in again before continuing.', { exact: true })).toBeVisible();
  expect(fixture.writes).toEqual([]);
  await expect(page.getByLabel('API token', { exact: true })).toHaveValue('');
});

test('anonymous company visitor is denied before record inspection is available', async ({ page, fixture }) => {
  interruptedRecovery(fixture);
  fixture.custom = async ({ url, method, send }) => {
    if (method === 'GET' && url.pathname === '/api/session') {
      await send({ message: 'Company sign in is required.' }, 401);
      return true;
    }
    return false;
  };
  await page.goto('/#/recovery/company-session-recovery');
  await expect(page.locator('#app')).toContainText('Company sign in is required.');
  await expect(page.locator('#app').getByRole('button', { name: 'Inspect and reconcile', exact: true })).toHaveCount(0);
  expect(fixture.writes).toEqual([]);
  await expect(page.getByLabel('API token', { exact: true })).toHaveValue('');
});
