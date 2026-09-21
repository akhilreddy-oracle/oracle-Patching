import { test, expect } from './fixtures.mjs';

test('artifact remediation stays passive until an operator explicitly probes and a viewer cannot probe', async ({ page, fixture }) => {
  const inspection = fixture.steps.find(step => step.step === 'artifact-inspect');
  inspection.status = 'blocked';
  inspection.evidence.artifact.status = 'blocked';
  inspection.evidence.artifact.reason = 'artifact is incomplete';
  fixture.session = { mode: 'principal', actor: 'fixture-operator', roles: ['operator'], rbac_enabled: true,
    permissions: { live_discovery: true } };
  const probeRequests = [];
  fixture.custom = async ({ request, url, method, send }) => {
    if (url.pathname !== '/api/hosts/source/artifact-sources') return false;
    probeRequests.push({ method, body: method === 'POST' ? request.postDataJSON() : null });
    await send({ targets: [{ node: 'source', state: 'incomplete' }],
      sources: [{ host_id: 'media-host', label: 'Media host', state: 'complete' }] });
    return true;
  };

  await page.goto('/#/hosts/source/readiness');
  const remediation = page.locator('#app .remediation');
  const probe = remediation.getByRole('button', { name: 'Probe managed hosts', exact: true });
  await expect(probe).toBeEnabled();
  await expect(remediation).toContainText('all other configured source hosts');
  await expect(remediation.getByRole('combobox', { name: /^Source host / })).toHaveValue('');
  expect(probeRequests).toEqual([]);
  expect(fixture.writes).toEqual([]);

  await probe.click();
  await expect(remediation.getByRole('combobox', { name: /^Source host / })).toHaveValue('media-host');
  expect(probeRequests).toEqual([{ method: 'POST', body: { artifact_dir: '/fixture/stage/39034528' } }]);
  expect(fixture.writes).toHaveLength(1);

  fixture.session = { mode: 'principal', actor: 'fixture-viewer', roles: ['viewer'], rbac_enabled: true,
    permissions: { live_discovery: false } };
  await page.reload();
  await expect(probe).toBeDisabled();
  await expect(remediation.getByRole('button', { name: 'Stage media', exact: true })).toBeDisabled();
  await expect(remediation).toContainText('Your account cannot run live discovery');
  expect(probeRequests).toHaveLength(1);
  expect(fixture.writes).toHaveLength(1);
});
