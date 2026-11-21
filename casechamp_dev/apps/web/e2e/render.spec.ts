import { writeFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import { heading, link, start } from './helpers';

test('Локальная задержка между ответом и отрисовкой каталога и сводки', async ({ page, browser }, info) => {
  await start(page);
  const stats = [];
  for (const screen of [
    { name: 'Каталог', url: '/api/v2/catalog?limit=50', selector: '.catalog-list .catalog-row' },
    { name: 'Рабочий стол', url: '/api/v2/dashboard', selector: '.metrics .metric' },
  ]) {
    const other = screen.name === 'Каталог' ? 'Рабочий стол' : 'Каталог';
    const samples: number[] = [];
    for (let iteration = 0; iteration < 30; iteration++) {
      await link(page, other).click();
      await expect(heading(page, other)).toBeVisible();
      const mark = await page.evaluate(() => performance.now());
      await link(page, screen.name).click();
      const ms = await page.evaluate(({ url, selector, mark }) => new Promise<number>((resolve, reject) => {
        const started = performance.now();
        const endpoint = new URL(url, location.origin).href;
        function frame() {
          const timings = performance.getEntriesByName(endpoint, 'resource') as PerformanceResourceTiming[];
          const response = [...timings].reverse().find(item => item.responseEnd > mark);
          if (response && document.querySelector(selector)) {
            resolve(performance.now() - response.responseEnd);
          } else if (performance.now() - started < 4_000) requestAnimationFrame(frame);
          else reject(new Error('Экран не обновился после ответа'));
        }
        requestAnimationFrame(frame);
      }), { ...screen, mark });
      if (iteration >= 5) samples.push(ms);
    }
    samples.sort((a, b) => a - b);
    const percentile = (p: number) => Number(samples[Math.ceil(p * samples.length) - 1]!.toFixed(2));
    stats.push({ screen: screen.name, samples: samples.length, p50_ms: percentile(.5), p95_ms: percentile(.95) });
  }
  const report = JSON.stringify({ chromium: browser.version(), viewport: page.viewportSize(), seed: 'CORE-001',
    measure: 'responseEnd to frame with rendered data', workers: 1, warmed_up_per_screen: 5,
    threshold_p95_ms: 200, stats }, null, 2) + '\n';
  await info.attach('render-latency.json', { body: report, contentType: 'application/json' });
  if (process.env.CASECHAMP_RENDER_REPORT) await writeFile(process.env.CASECHAMP_RENDER_REPORT, report);
  for (const item of stats) expect(item.p95_ms).toBeLessThanOrEqual(200);
});
