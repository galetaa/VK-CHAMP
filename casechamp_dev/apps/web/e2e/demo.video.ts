import { execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join as pathJoin, resolve } from 'node:path';
import { expect, test } from '@playwright/test';
import type { Browser, BrowserContext, Page } from '@playwright/test';
import { button, heading, join, publish, root, start, total } from './helpers';

type SavedSession = Awaited<ReturnType<BrowserContext['storageState']>>;
const viewport = { width: 1200, height: 850 };

async function record<T>(browser: Browser, folder: string, chapter: string,
  callback: (page: Page) => Promise<T>, storageState?: SavedSession) {
  const context = await browser.newContext({ baseURL: root(), storageState, viewport,
    recordVideo: { dir: folder, size: viewport } });
  const page = await context.newPage();
  try {
    return await callback(page);
  } finally {
    await context.close();
    await page.video()?.saveAs(pathJoin(folder, chapter + '.webm'));
  }
}

test('Демонстрационная запись CORE-01', async ({ browser }) => {
  const folder = await mkdtemp(pathJoin(tmpdir(), 'casechamp-d1-video-'));
  try {
    const seller = await record(browser, folder, 'owner', async page => {
      await page.goto('/');
      await expect(heading(page, 'Первый заказ из сообщества')).toBeVisible();
      await page.waitForTimeout(1600);
      await start(page);
      await page.waitForTimeout(1300);
      const offer = await publish(page, 'Чайный набор');
      await page.waitForTimeout(2100);
      return { offer, session: await page.context().storageState() };
    });
    const setup = await browser.newContext({ baseURL: root(), storageState: seller.session });
    const owner = await setup.newPage();
    const buyer = await join(owner, browser, 'покупателя');
    const operator = await join(owner, browser, 'оператора');
    const buyerSession = await buyer.context.storageState();
    const operatorSession = await operator.context.storageState();
    await Promise.all([setup.close(), buyer.context.close(), operator.context.close()]);
    await record(browser, folder, 'buyer', async page => {
      await page.goto(seller.offer.share);
      await expect(heading(page, 'Чайный набор')).toBeVisible();
      await page.waitForTimeout(1700);
      await page.getByLabel(/Способ получения/).selectOption('COURIER_ZONE_A');
      await button(page, 'Проверить условия').click();
      await total(page, '2 490,00 ₽');
      await page.waitForTimeout(2100);
      await button(page, 'Зарезервировать на 5 минут').click();
      await expect(heading(page, 'Резерв товаров')).toBeVisible();
      await page.waitForTimeout(2000);
      await button(page, 'Подтвердить тестовый заказ').click();
      await expect(heading(page, 'Тестовый заказ приложения')).toBeVisible();
      await page.waitForTimeout(2200);
    }, buyerSession);
    await record(browser, folder, 'operator', async page => {
      await page.goto('/app/orders');
      await page.locator('article[data-order-id]').getByRole('link').click();
      await expect(heading(page, 'Тестовый заказ приложения')).toBeVisible();
      await page.waitForTimeout(1400);
      await button(page, 'Принять').click();
      await expect(page.locator('.tag')).toHaveText('Принят');
      await page.waitForTimeout(1400);
      await button(page, 'Отметить исполненным').click();
      await expect(page.locator('.tag')).toHaveText('Исполнен');
      await page.waitForTimeout(2600);
    }, operatorSession);
    const list = pathJoin(folder, 'chapters.txt');
    await writeFile(list, ['owner', 'buyer', 'operator'].map(name => "file '" + name + ".webm'").join('\n'));
    const video = resolve(process.env.CASECHAMP_DEMO_VIDEO ?? '../../reports/frontend-v2/core-demo.mp4');
    await mkdir(dirname(video), { recursive: true });
    execFileSync('ffmpeg', ['-loglevel', 'error', '-y', '-f', 'concat', '-i', list, '-vf',
      'fps=25', '-c:v', 'libx264', '-preset', 'fast', '-crf', '22', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', video]);
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
});
