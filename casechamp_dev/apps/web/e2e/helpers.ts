import { expect } from '@playwright/test';
import type { Browser, Page, TestInfo } from '@playwright/test';

export const heading = (page: Page, name: string | RegExp) => page.getByRole('heading', { level: 1, name });
export const button = (page: Page, name: string | RegExp) => page.getByRole('button', { name, exact: typeof name === 'string' });
export const link = (page: Page, name: string) => page.getByRole('link', { name, exact: true });
export const total = (page: Page, value: string) => expect(page.getByTestId('total')).toHaveText(value);
export const identity = (page: Page, role: string) => expect(page.locator('.identity b')).toHaveText(role);
export const root = () => process.env.CASECHAMP_TEST_ORIGIN ?? 'http://localhost:18081';

export async function start(page: Page, bundle = false) {
  await page.goto('/');
  if (bundle) await page.getByLabel('Добавить демонстрацию подбора').check();
  await button(page, 'Создать демонстрацию').click();
  await expect(heading(page, 'Рабочий стол')).toBeVisible();
  await identity(page, 'Владелец');
}

export async function join(owner: Page, browser: Browser, role: 'покупателя' | 'оператора', viewport?: { width: number; height: number }) {
  await owner.goto('/app/demo');
  const section = owner.locator('section').filter({ has: owner.getByRole('heading', {
    name: role === 'покупателя' ? 'Вход покупателя' : 'Вход оператора', exact: true,
  }) });
  const issued = owner.waitForResponse(res => res.url().endsWith('/api/v2/demo/role-links') && res.request().method() === 'POST');
  await section.getByRole('button', { name: 'Создать ссылку ' + role }).click();
  expect((await issued).status()).toBe(201);
  const input = section.getByRole('textbox');
  await expect(input).toBeVisible();
  const url = await input.inputValue();
  const token = new URL(url).hash.slice('#token='.length);
  const context = await browser.newContext({ baseURL: root(), viewport: viewport ?? { width: 1440, height: 960 } });
  const user = await context.newPage();
  await user.goto(url).catch(() => { throw new Error('Не удалось открыть приглашение'); });
  await expect(heading(user, 'Вход по приглашению')).toBeVisible();
  expect(new URL(user.url()).hash.length).toBe(0);
  expect(await user.evaluate(secret => [localStorage, sessionStorage].every(store =>
    Object.keys(store).every(key => !(store.getItem(key) ?? '').includes(secret))), token)).toBe(true);
  await button(user, 'Войти по ссылке').click();
  await identity(user, role === 'покупателя' ? 'Покупатель' : 'Оператор');
  expect(await user.evaluate(secret => [localStorage, sessionStorage].every(store =>
    Object.keys(store).every(key => !(store.getItem(key) ?? '').includes(secret))), token)).toBe(true);
  return { user, context };
}

export async function draft(page: Page, title: string, mode: 'FIXED' | 'BUNDLE' = 'FIXED') {
  await page.goto('/app/offers/new');
  await button(page, mode === 'FIXED' ? 'Создать черновик' : 'Создать предложение с подбором').click();
  await expect(heading(page, 'Мастер предложения')).toBeVisible();
  await page.getByLabel('Название для публикации (обязательно)').fill(title);
  await page.getByLabel('Описание', { exact: true }).fill('Демонстрационный состав для испытания заказа.');
  await button(page, 'Далее').click();
  if (mode === 'FIXED') {
    await button(page, 'Добавить MUG').click();
    await expect(page.getByLabel('Количество MUG', { exact: true })).toBeFocused();
    await button(page, 'Добавить TEA').click();
    await page.getByLabel('Количество TEA', { exact: true }).fill('2');
  } else {
    const select = page.getByLabel('Шаблон комплекта (обязательно)');
    await expect(select.locator('option')).toHaveCount(2);
    await select.selectOption({ index: 1 });
    await expect(page.getByText(/контрольный шаблон только для чтения/)).toBeVisible();
  }
  await button(page, 'Далее').click();
  await page.getByRole('checkbox', { name: /Курьер/ }).check();
  await button(page, 'Далее').click();
  return new URL(page.url()).pathname;
}

export async function publish(page: Page, title: string, mode: 'FIXED' | 'BUNDLE' = 'FIXED') {
  const editing = await draft(page, title, mode);
  await button(page, 'К предпросмотру').click();
  await expect(heading(page, 'Предпросмотр предложения')).toBeVisible();
  await expect(page.getByText('Предпросмотр · заказ недоступен')).toBeVisible();
  await expect(button(page, /резерв/i)).toHaveCount(0);
  await button(page, 'Опубликовать в демонстрации').click();
  await expect(heading(page, title)).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Опубликованная версия 1' })).toBeVisible();
  const share = await page.getByRole('textbox', { name: 'Готовая ссылка' }).inputValue();
  expect(new URL(share).origin).toBe(root());
  return { share, editing, detail: editing.replace(/\/edit$/, ''), id: new URL(share).pathname.split('/').at(-1)! };
}

export async function evidence(page: Page, info: TestInfo, name: string) {
  const folder = process.env.CASECHAMP_EVIDENCE_DIR;
  if (folder) await page.screenshot({ path: folder + '/' + name + '.png', fullPage: true, animations: 'disabled' });
  else await info.attach(name, { body: await page.screenshot({ fullPage: true, animations: 'disabled' }), contentType: 'image/png' });
}

export async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(page.viewportSize()!.width);
}
