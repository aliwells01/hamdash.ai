import { chromium } from '@playwright/test';

const GRID = process.env.GRID || 'EM84ab';        // ← change if you like
const URL  = `https://hf.dxview.org/perspective/${GRID}`;
const OUT  = process.env.OUT || 'dxview.png';
const WIDTH  = parseInt(process.env.WIDTH  || '1280', 10);
const HEIGHT = parseInt(process.env.HEIGHT || '720', 10);

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: WIDTH, height: HEIGHT }, deviceScaleFactor: 2 });
  await page.goto(URL, { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(2000); // give tiles a sec to render
  await page.screenshot({ path: OUT, fullPage: true });
  await browser.close();
})().catch(err => { console.error(err); process.exit(1); });
