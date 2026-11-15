/* Real Chromium smoke for the plugin's OpenUI shell; synthetic data only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch (error) {
  console.error(`SKIP: Playwright is unavailable: ${error.message}`);
  process.exit(77);
}

const baseUrl = process.env.HTE_OPENUI_BROWSER_BASE_URL;
const views = JSON.parse(process.env.HTE_OPENUI_BROWSER_VIEWS || '{}');
const evidence = process.env.HTE_OPENUI_BROWSER_EVIDENCE || '/opt/data/cache/scratch/openui-step1/evidence/round5';
if (!baseUrl || !views.light || !views.dark) {
  throw new Error('HTE_OPENUI_BROWSER_BASE_URL and light/dark HTE_OPENUI_BROWSER_VIEWS are required');
}
fs.mkdirSync(evidence, { recursive: true });

async function run() {
  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || undefined,
      args: ['--no-sandbox'],
    });
  } catch (error) {
    console.error(`SKIP: Chromium is unavailable: ${error.message}`);
    process.exitCode = 77;
    return;
  }
  const reports = [];
  try {
    for (const theme of ['light', 'dark']) {
      const view = views[theme];
      const consoleErrors = [];
      const pageErrors = [];
      const context = await browser.newContext({
        colorScheme: theme,
        viewport: { width: 390, height: 844 },
        deviceScaleFactor: 1,
      });
      await context.route('https://telegram.org/**', (route) => route.fulfill({
        status: 200,
        contentType: 'application/javascript',
        body: '/* synthetic Telegram bridge */',
      }));
      await context.addInitScript(({ initData, colorScheme }) => {
        window.__hteCspViolations = [];
        document.addEventListener('securitypolicyviolation', (event) => {
          window.__hteCspViolations.push(event.effectiveDirective);
        });
        window.Telegram = { WebApp: {
          initData,
          colorScheme,
          themeParams: {},
          ready() {},
          expand() {},
          onEvent() {},
        } };
      }, { initData: view.initData, colorScheme: theme });
      const page = await context.newPage();
      page.on('console', (message) => {
        if (message.type() === 'error') consoleErrors.push(message.text());
      });
      page.on('pageerror', (error) => pageErrors.push(String(error)));
      const response = await page.goto(`${baseUrl}/m?id=${encodeURIComponent(view.id)}`, { waitUntil: 'networkidle' });
      assert.equal(response.status(), 200);
      const csp = response.headers()['content-security-policy'] || '';
      assert.match(csp, /style-src 'self'/);
      assert.match(csp, /img-src 'self' data:/);
      assert.equal(csp.includes('unsafe-inline'), false);

      await page.getByRole('tab', { name: 'Overview' }).waitFor();
      const shell = await page.evaluate(() => ({
        bodyMargin: getComputedStyle(document.body).margin,
        bodyBackground: getComputedStyle(document.body).backgroundColor,
        titleFont: getComputedStyle(document.querySelector('#title')).fontFamily,
        retryHeight: getComputedStyle(document.querySelector('#retry')).minHeight,
        rendererCss: Boolean([...document.styleSheets].find((sheet) => sheet.href?.endsWith('/m/static/openui.css'))),
      }));
      assert.equal(shell.bodyMargin, '0px');
      assert.notEqual(shell.bodyBackground, 'rgba(0, 0, 0, 0)');
      assert.notEqual(shell.titleFont, 'serif');
      assert.equal(shell.retryHeight, '44px');
      assert.equal(shell.rendererCss, true);
      assert.equal(await page.locator('.openui-miniapp').count(), 1);

      await page.getByRole('tab', { name: 'Spending' }).click();
      await page.locator('tbody tr').first().waitFor();
      const componentStyles = await page.evaluate(() => {
        const row = document.querySelector('tbody tr');
        const chartText = [...document.querySelectorAll('[data-openui="bar-chart"] text')].map((node) => node.textContent);
        const tabs = [...document.querySelectorAll('[role="tab"]')].map((tab) => {
          const style = getComputedStyle(tab);
          return { background: style.backgroundColor, borderTop: style.borderTopWidth, borderLeft: style.borderLeftWidth };
        });
        return { divider: getComputedStyle(row).borderBottomWidth, chartText, tabs };
      });
      assert.equal(componentStyles.divider, '1px');
      assert.ok(componentStyles.chartText.includes('-$50'), JSON.stringify(componentStyles.chartText));
      for (const tab of componentStyles.tabs) {
        assert.ok(tab.background === 'rgba(0, 0, 0, 0)' || tab.background === 'transparent', JSON.stringify(tab));
        assert.equal(tab.borderTop, '0px');
        assert.equal(tab.borderLeft, '0px');
      }

      await page.getByRole('tab', { name: 'Review' }).click();
      const checkboxes = page.locator('input[name="transaction_ids"][type="checkbox"]');
      await checkboxes.first().check();
      await page.getByRole('button', { name: 'Submit review' }).click();
      await page.getByText('Recorded.', { exact: true }).waitFor();
      assert.equal(await page.getByRole('tab', { name: 'Review' }).getAttribute('aria-selected'), 'true');
      assert.equal(await checkboxes.count(), 2);
      assert.equal(await checkboxes.first().isChecked(), true);
      assert.equal(await page.locator('[data-openui="bar-chart"]').count(), 1);
      assert.equal(await page.evaluate(() => window.__hteCspViolations.length), 0);
      assert.deepEqual(consoleErrors, []);
      assert.deepEqual(pageErrors, []);
      const screenshot = path.join(evidence, `openui-finance-${theme}-390x844-after-submit.png`);
      await page.screenshot({ path: screenshot, fullPage: true });
      reports.push({ theme, csp, shell, componentStyles, screenshot });
      await context.close();
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ passed: true, reports }, null, 2));
}

run().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
