import { test, expect } from './fixtures.mjs';

test.use({ seedLabCredentials: false });

for (const viewport of [{ width: 1174, height: 606 }, { width: 390, height: 640 }]) {
  test(`visible sign-in recovers from invalid credentials at ${viewport.width}x${viewport.height}`, async ({ page, fixture }) => {
    await page.setViewportSize(viewport);
    fixture.session = { actor: 'personal-fixture-user', roles: ['requester'], rbac_enabled: true };
    fixture.custom = async ({ request, url, method, send }) => {
      if (method === 'GET' && ['/api/session', '/api/estate'].includes(url.pathname)
        && request.headers().authorization !== 'Bearer personal-fixture-token') {
        await send({ message: 'Missing or invalid principal credential' }, 401);
        return true;
      }
      return false;
    };
    await page.goto('/#/plans');
    const main = page.locator('#app');
    const input = main.getByLabel('Sign-in API token', { exact: true });
    const signIn = main.getByRole('button', { name: 'Sign in', exact: true });
    await expect(main.getByRole('heading', { name: 'Sign in to continue' })).toBeVisible();
    await expect(input).toBeInViewport(); await expect(signIn).toBeInViewport();
    await expect(input).toHaveAttribute('type', 'password');
    await input.fill('old-shared-token');
    expect(await page.evaluate(() => localStorage.getItem('opu-webapp-token'))).toBeNull();
    await signIn.click();
    await expect(input).toHaveValue('');
    await expect(main).toContainText('Missing or invalid principal credential');
    await input.fill('personal-fixture-token'); await signIn.click();
    await expect(main.getByRole('heading', { name: 'All plans', exact: true })).toBeVisible();
    await expect(page.locator('#rail-session')).toContainText('Authenticated as personal-fixture-user');
    await expect(page.getByLabel('API token', { exact: true })).toHaveValue('personal-fixture-token');
    await expect(page.getByLabel('Acting as', { exact: true })).toHaveAttribute('readonly', '');
    expect(page.url()).not.toContain('personal-fixture-token');
    expect(await page.locator('body').innerText()).not.toContain('personal-fixture-token');
    expect(fixture.writes).toEqual([]);
  });
}

test('an expired company session shows company sign-in beside the failure', async ({ page, fixture }) => {
  fixture.custom = async ({ url, method, send }) => {
    if (method === 'GET' && url.pathname === '/api/auth/config') {
      await send({ configured: true, login_url: '/api/auth/login', label: 'Company sign in' }); return true;
    }
    if (method === 'GET' && url.pathname === '/api/session') {
      await send({ message: 'Company sign in is required.' }, 401); return true;
    }
    return false;
  };
  await page.goto('/#/plans');
  const company = page.locator('#app').getByRole('link', { name: 'Company sign in', exact: true });
  await expect(company).toBeVisible(); await expect(company).toHaveAttribute('href', '/api/auth/login');
  expect(fixture.writes).toEqual([]);
});
