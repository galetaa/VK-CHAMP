import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { DTO } from '../src/app/api';
import { button, heading, join, link, publish, start } from './helpers';

const quote = async (page: Page) => {
  await button(page, 'Проверить условия').click();
  await expect(heading(page, 'Проверка полной стоимости')).toBeVisible();
};
const reserve = (page: Page) => button(page, 'Зарезервировать на 5 минут').click();

test('UI-09/10/12 · серверные часы, точное истечение и GET после replay резерва', async ({ page, browser }) => {
  await start(page);
  const offer = await publish(page, 'Проверка срока');
  const buyer = await join(page, browser, 'покупателя');
  try {
    const reads: number[] = [];
    const expiredQuote = '**/api/v2/quotes/*';
    await buyer.user.route(expiredQuote, async route => {
      const result = await route.fetch();
      const json = await result.json();
      json.meta.server_now = json.data.expires_at;
      reads.push(result.status());
      await route.fulfill({ response: result, json });
    });
    await buyer.user.goto(offer.share);
    await quote(buyer.user);
    await expect(buyer.user.getByText('Время расчёта истекло.', { exact: true })).toBeVisible();
    await expect(button(buyer.user, 'Зарезервировать на 5 минут')).toBeDisabled();
    await expect.poll(() => reads.length).toBeGreaterThanOrEqual(2);
    await buyer.user.unroute(expiredQuote);
    await buyer.user.goto(offer.share);
    await quote(buyer.user);

    let seen = 0;
    const reservePattern = '**/api/v2/reservations';
    await buyer.user.route(reservePattern, async route => {
      const answer = await route.fetch();
      expect(answer.status()).toBe(201);
      if (++seen === 1) await route.abort('connectionreset');
      else {
        const original = await answer.json();
        expect(original.meta.replayed).toBe(true);
        expect(original.data.state).toBe('ACTIVE');
        await route.fulfill({ response: answer, json: original });
      }
    });
    await reserve(buyer.user);
    await expect(button(buyer.user, 'Проверить выполнение')).toBeEnabled();
    const readPattern = '**/api/v2/reservations/*';
    let getCount = 0;
    await buyer.user.route(readPattern, async route => {
      const answer = await route.fetch();
      const current = await answer.json();
      getCount++;
      await route.fulfill({ response: answer, json: {
        data: { ...current.data, state: 'EXPIRED', revision: current.data.revision + 1, closed_at: current.data.expires_at },
        meta: { ...current.meta, server_now: current.data.expires_at },
      } });
    });
    await button(buyer.user, 'Проверить выполнение').click();
    await expect(heading(buyer.user, 'Резерв товаров')).toBeVisible();
    await expect(buyer.user.getByText('Время резерва истекло. Товары освобождены.')).toBeVisible();
    await expect(button(buyer.user, 'Подтвердить тестовый заказ')).toHaveCount(0);
    await expect(link(buyer.user, 'Проверить условия заново')).toBeVisible();
    expect(getCount).toBeGreaterThanOrEqual(2);
    expect(seen).toBe(2);
    await buyer.user.unrouteAll();
    await buyer.user.reload();
    await expect(button(buyer.user, 'Подтвердить тестовый заказ')).toBeEnabled();
    await button(buyer.user, 'Отменить резерв').click();
    await buyer.user.getByRole('dialog').getByRole('button', { name: 'Отменить резерв', exact: true }).click();
    await expect(buyer.user.getByText('Резерв отменён.', { exact: true })).toBeVisible();
  } finally { await buyer.context.close(); }
});

async function catalog(owner: Page) {
  const result = await owner.request.get('/api/v2/catalog');
  expect(result.ok()).toBe(true);
  const body: { data: DTO<'CatalogPage'> } = await result.json();
  return body.data.items.filter(item => ['MUG', 'TEA'].includes(item.sku))
    .map(({ sku, stock_on_hand, stock_reserved }) => [sku, stock_on_hand, stock_reserved]);
}
test('UI-02/13/14/18 · очередь, конкурирующая отмена, остатки и события', async ({ page, browser }) => {
  await start(page);
  const offer = await publish(page, 'Состав заказа');
  const buyer = await join(page, browser, 'покупателя');
  const other = await join(page, browser, 'покупателя');
  const operator = await join(page, browser, 'оператора');
  try {
    const eventPromise = buyer.user.waitForRequest(req => req.method() === 'POST' && req.url().endsWith('/api/v2/ui-events'));
    await buyer.user.goto(offer.share);
    const event = (await eventPromise).postDataJSON();
    expect(Object.keys(event).sort()).toEqual(['event_id', 'name', 'offer_id', 'offer_version_no', 'ui_step']);
    expect(event.name).toBe('offer_view');
    expect(event.offer_id).toBe(offer.id);
    await page.goto('/app');
    await expect(page.getByText('Создано заказов', { exact: true }).locator('..').locator('b')).toHaveText('0');
    await expect(page.getByText(/Внутренние заказы VK не измеряются/)).toBeVisible();
    expect(await catalog(page)).toEqual([['MUG', 10, 0], ['TEA', 20, 0]]);
    const orderIds: string[] = [];
    for (let turn = 0; turn < 2; turn++) {
      await buyer.user.goto(offer.share);
      await quote(buyer.user);
      const buyerQuote = new URL(buyer.user.url()).pathname;
      if (!turn) {
        await other.user.goto(buyerQuote);
        await expect(heading(other.user, 'Эта страница недоступна в текущей демонстрации')).toBeVisible();
      }
      await reserve(buyer.user);
      await expect(heading(buyer.user, 'Резерв товаров')).toBeVisible();
      if (!turn) expect(await catalog(page)).toEqual([['MUG', 10, 1], ['TEA', 20, 2]]);
      await button(buyer.user, 'Подтвердить тестовый заказ').click();
      await expect(heading(buyer.user, 'Тестовый заказ приложения')).toBeVisible();
      const path = new URL(buyer.user.url()).pathname;
      orderIds.push(path.split('/').at(-1)!);
      const response = await buyer.user.request.get('/api/v2/orders/' + orderIds.at(-1));
      const { data } = await response.json();
      expect([data.order_kind, data.is_test, data.native_vk_order_id, data.paid_at]).toEqual(['DEMO', true, null, null]);
      if (!turn) expect(await catalog(page)).toEqual([['MUG', 9, 0], ['TEA', 18, 0]]);
    }
    await other.user.goto('/buy/orders/' + orderIds[0]);
    await expect(heading(other.user, 'Эта страница недоступна в текущей демонстрации')).toBeVisible();
    const path = '/app/orders/' + orderIds[0];
    await page.goto(path);
    let saved: { data: DTO<'Order'> } | undefined;
    await operator.user.route('**/api/v2/orders/' + orderIds[0], async route => {
      const response = await route.fetch();
      const actual = await response.json();
      saved ??= actual;
      await route.fulfill({ response, json: saved });
    });
    await operator.user.goto(path);
    await expect(button(operator.user, 'Отменить')).toBeVisible();
    await button(operator.user, 'Отменить').click();
    await expect(button(operator.user, 'Отменить заказ')).toBeDisabled();
    await operator.user.keyboard.press('Escape');
    await expect(button(operator.user, 'Отменить')).toBeFocused();
    await button(operator.user, 'Отменить').click();
    await operator.user.getByLabel(/Причина отмены/).selectOption('CUSTOMER_REQUEST');

    await button(page, 'Отменить').click();
    await page.getByLabel(/Причина отмены/).selectOption('DEMO_CORRECTION');
    await button(page, 'Отменить заказ').click();
    await expect(page.locator('.tag')).toHaveText('Отменён');
    await operator.user.unroute('**/api/v2/orders/' + orderIds[0]);
    await button(operator.user, 'Отменить заказ').click();
    await expect(operator.user.getByRole('alert')).toContainText('Заказ уже обработан.');
    await expect(operator.user.locator('.tag')).toHaveText('Отменён');
    await expect(button(operator.user, 'Отменить')).toHaveCount(0);
    expect(await catalog(page)).toEqual([['MUG', 9, 0], ['TEA', 18, 0]]);

    await operator.user.route(/\/api\/v2\/orders\?/, async route => {
      const request = new URL(route.request().url());
      request.searchParams.set('limit', '1');
      await route.continue({ url: request.toString() });
    });
    await operator.user.goto('/app/orders');
    const [first, last] = [...orderIds].sort();
    await expect(operator.user.locator('article[data-order-id]')).toHaveAttribute('data-order-id', first!);
    await button(operator.user, 'Следующая страница').click();
    await expect(operator.user.locator('article[data-order-id]')).toHaveAttribute('data-order-id', last!);
    await expect(button(operator.user, 'Следующая страница')).toHaveCount(0);
    await button(operator.user, 'Предыдущая страница').click();
    await expect(operator.user.locator('article[data-order-id]')).toHaveAttribute('data-order-id', first!);
  } finally {
    await buyer.context.close();
    await other.context.close();
    await operator.context.close();
  }
});
