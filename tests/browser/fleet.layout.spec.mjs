import { test, expect } from './fixtures.mjs';

const longHome = '/u01/app/oracle/product/19.0.0.0/dbhome_finance_reporting_and_enterprise_analytics_01';
const longHost = 'Oracle production finance reporting and enterprise analytics database server';
const patchIds = '29517242, 29585399, 30557433, 31281355, 34765931, 39034528';

for (const width of [1280, 1024, 768, 390]) {
  test(`fleet remains readable and keyboard accessible at ${width}px with long inventory`, async ({ page, fixture }, testInfo) => {
    await page.setViewportSize({ width, height: 960 });
    fixture.estate.hosts[0].label = longHost;
    fixture.fleet.can_manage_metadata = true;
    fixture.fleet.databases = [{ ...fixture.fleet.databases[0],
      host_label: longHost, oracle_home: longHome, patch_baseline: patchIds,
      metadata_version: 'c'.repeat(64), configuration_missing: [],
    }];
    await page.goto('/#/estate');
    const fleet = page.locator('#app .fleet-dashboard');
    await expect(fleet.getByRole('heading', { name: 'Fleet compliance', exact: true })).toBeVisible();
    const fields = fleet.locator('.fleet-filters > label');
    await expect(fields).toHaveCount(7);
    const filterGeometry = await fields.evaluateAll(labels => labels.map(label => {
      const rect = node => {
        const { x, y, width, height } = node.getBoundingClientRect();
        return { x, y, width, height };
      };
      return { label: rect(label), caption: rect(label.querySelector('span')), select: rect(label.querySelector('select')) };
    }));
    for (const field of filterGeometry) {
      expect(field.select.y, 'Filter caption must sit above its control').toBeGreaterThanOrEqual(field.caption.y + field.caption.height + 2);
      expect(Math.abs(field.select.x - field.label.x), 'Control left edge aligns with its field').toBeLessThanOrEqual(1);
      expect(Math.abs(field.select.width - field.label.width), 'Control fills its own grid field').toBeLessThanOrEqual(2);
      expect(field.select.x + field.select.width, 'Filter stays inside the viewport').toBeLessThanOrEqual(width + 1);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth), 'Horizontal overflow belongs inside the table, not the document').toBeLessThanOrEqual(width + 1);
    const scrollRegion = fleet.getByRole('region', { name: 'Fleet databases', exact: true });
    await expect(scrollRegion).toHaveAttribute('tabindex', '0');
    const regionSize = await scrollRegion.evaluate(node => ({ viewport: node.clientWidth, content: node.scrollWidth, left: node.scrollLeft }));
    expect(regionSize.left, 'Inventory starts at the database column').toBe(0);
    if (width <= 620) expect(regionSize.content, 'Mobile inventory cards fit their container').toBeLessThanOrEqual(regionSize.viewport + 1);
    else {
      expect(regionSize.content, 'Wide inventory scrolls within its named region').toBeGreaterThan(regionSize.viewport);
      const headerGeometry = await scrollRegion.getByRole('columnheader', { name: 'Environment', exact: true }).evaluate(header => {
        const range = document.createRange();
        range.selectNodeContents(header);
        const style = getComputedStyle(header);
        return {
          fragments: [...range.getClientRects()].filter(rect => rect.width > 0 && rect.height > 0).length,
          textWidth: range.getBoundingClientRect().width,
          availableWidth: header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
        };
      });
      expect(headerGeometry.fragments, 'Environment heading must not wrap within the word').toBe(1);
      expect(headerGeometry.textWidth, 'Environment heading fits its cell content width').toBeLessThanOrEqual(headerGeometry.availableWidth + 1);
    }

    const row = fleet.locator('tbody tr');
    await expect(row).toHaveCount(1);
    const firstCell = row.locator('td').first();
    await expect(firstCell).toContainText(longHost);
    await expect(firstCell).toContainText(longHome);
    const cell = await firstCell.boundingBox();
    const path = await firstCell.locator('.mono').boundingBox();
    expect(cell.x).toBeGreaterThanOrEqual(0);
    expect(cell.x + cell.width, 'Database column must be accessible before horizontal scrolling').toBeLessThanOrEqual(width + 1);
    expect(path.x).toBeGreaterThanOrEqual(cell.x);
    expect(path.x + path.width, 'Long Oracle home wraps within the database cell').toBeLessThanOrEqual(cell.x + cell.width + 1);
    await page.screenshot({ path: testInfo.outputPath(`fleet-${width}-overview.png`), fullPage: true });

    // Enter the row using the real tab order. A scroll region may be a tab stop;
    // table action links must still be reachable and scrolled into view.
    await fleet.getByLabel('Evidence freshness', { exact: true }).focus();
    const lastAction = row.getByRole('link', { name: 'Review readiness', exact: true });
    let reached = false;
    for (let index = 0; index < 8; index++) {
      await page.keyboard.press('Tab');
      if (await lastAction.evaluate(node => node === document.activeElement)) { reached = true; break; }
    }
    expect(reached, 'Keyboard reaches the rightmost action without scrolling by mouse').toBe(true);
    const action = await lastAction.boundingBox();
    expect(action.x).toBeGreaterThanOrEqual(0);
    expect(action.x + action.width, 'Focused action must be visible horizontally').toBeLessThanOrEqual(width + 1);
    expect(action.y).toBeGreaterThanOrEqual(0);
    expect(action.y + action.height, 'Focused action must be visible vertically').toBeLessThanOrEqual(961);
    if (width > 620) {
      const anchoredCell = await firstCell.boundingBox();
      expect(Math.abs(anchoredCell.x - cell.x), 'Database context stays anchored while navigating later columns').toBeLessThanOrEqual(1);
    }
    await page.keyboard.press('Tab');
    const settings = row.locator('summary').filter({ hasText: 'Edit fleet settings' });
    await expect(settings).toBeFocused();
    await page.keyboard.press('Enter');
    await page.keyboard.press('Tab');
    await expect(row.getByLabel('Environment for source', { exact: true })).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth), 'Opening metadata fields must not expand the document').toBeLessThanOrEqual(width + 1);
    expect(fixture.writes).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`fleet-${width}-keyboard.png`), fullPage: true });
  });
}
