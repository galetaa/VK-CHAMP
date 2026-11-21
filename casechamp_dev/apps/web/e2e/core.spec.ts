import { writeFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import type { DTO } from '../src/app/api';
import { button, evidence, heading, identity, join, noOverflow, publish, start, total } from './helpers';

test('CORE-01 · владелец, покупатель и оператор · потеря ответа confirm и сброс', async ({ page, browser }, info) => {
  await page.goto('/');
  await expect(heading(page, 'Первый заказ из сообщества')).toBeVisible();
  await evidence(page, info, '01-start-1440');
  await start(page);
  await evidence(page, info, '02-owner-dashboard-1440');
  const offer = await publish(page, 'Чайный набор');
  await evidence(page, info, '03-published-offer-1440');

  const buyer = await join(page, browser, 'покупателя', { width: 360, height: 800 });
  const operator = await join(page, browser, 'оператора', { width: 768, height: 900 });
  try {
    await operator.user.goto('/app/catalog');
    await expect(operator.user.getByText('Изменяет владелец.')).toBeVisible();
    await expect(button(operator.user, 'Новый SKU')).toHaveCount(0);
    await operator.user.goto('/app/demo');
    await expect(heading(operator.user, 'Эта страница недоступна в текущей демонстрации')).toBeVisible();
    await operator.user.goto('/app/orders');
    await expect(operator.user.getByText('Заказов пока нет')).toBeVisible();
    await buyer.user.goto(offer.share);
    await expect(heading(buyer.user, 'Чайный набор')).toBeVisible();
    await noOverflow(buyer.user);
    await evidence(buyer.user, info, '04-buyer-offer-360');
    await buyer.user.getByLabel('Количество одинаковых предложений (обязательно)').fill('1.5');
    await button(buyer.user, 'Проверить условия').focus();
    await buyer.user.keyboard.press('Enter');
    await expect(buyer.user.getByText('Введите целое число от 1 до 10.')).toBeVisible();
    await expect(buyer.user.getByLabel('Количество одинаковых предложений (обязательно)')).toBeFocused();
    await buyer.user.keyboard.press('Control+a');
    await buyer.user.keyboard.type('1');
    const method = buyer.user.getByLabel('Способ получения (обязательно)');
    await buyer.user.keyboard.press('Tab');
    await expect(method).toBeFocused();
    await buyer.user.keyboard.press('End');
    await expect(method).toHaveValue('COURIER_ZONE_A');
    await buyer.user.keyboard.press('Tab');
    await buyer.user.keyboard.press('Tab');
    await expect(button(buyer.user, 'Проверить условия')).toBeFocused();
    await buyer.user.keyboard.press('Enter');
    await total(buyer.user, '2 490,00 ₽');
    await noOverflow(buyer.user);
    await evidence(buyer.user, info, '05-quote-360');
    const zoom = await buyer.user.addStyleTag({ content: 'html { font-size: 200% !important; }' });
    await total(buyer.user, '2 490,00 ₽');
    await noOverflow(buyer.user);
    await evidence(buyer.user, info, '06-quote-360-200pct');
    await button(buyer.user, 'Зарезервировать на 5 минут').focus();
    await buyer.user.keyboard.press('Enter');
    await expect(heading(buyer.user, 'Резерв товаров')).toBeVisible();
    await total(buyer.user, '2 490,00 ₽');
    await zoom.evaluate(node => node.parentNode?.removeChild(node));
    await evidence(buyer.user, info, '07-reservation-360');

    const attempts: { body: string | null; key: string | undefined }[] = [];
    const pattern = '**/api/v2/reservations/*/confirm';
    await buyer.user.route(pattern, async route => {
      const request = route.request();
      attempts.push({ body: request.postData(), key: request.headers()['idempotency-key'] });
      if (attempts.length === 1) {
        const response = await route.fetch();
        expect(response.status()).toBe(201);
        await route.abort('connectionreset');
      } else await route.continue();
    });
    const confirm = button(buyer.user, 'Подтвердить тестовый заказ');
    await confirm.focus();
    await buyer.user.keyboard.press('Enter');
    await buyer.user.keyboard.press('Enter');
    await expect(button(buyer.user, 'Проверить выполнение')).toBeEnabled();
    expect(attempts).toHaveLength(1);
    expect(attempts[0]?.key).toMatch(/^[a-f0-9-]{36}$/);
    await buyer.user.reload();
    await expect(button(buyer.user, 'Проверить выполнение')).toBeEnabled();
    await expect(button(buyer.user, 'Подтвердить тестовый заказ')).toHaveCount(0);
    const freshOrder = buyer.user.waitForResponse(res => /\/api\/v2\/orders\/[^/]+$/.test(res.url()) && res.request().method() === 'GET');
    await button(buyer.user, 'Проверить выполнение').click();
    expect((await freshOrder).status()).toBe(200);
    expect(attempts).toHaveLength(2);
    expect(attempts[0]).toEqual(attempts[1]);
    await expect(heading(buyer.user, 'Тестовый заказ приложения')).toBeVisible();
    await total(buyer.user, '2 490,00 ₽');
    await expect(buyer.user.getByText('Платёж не выполнялся.')).toBeVisible();
    const buyerOrder = new URL(buyer.user.url()).pathname;
    await evidence(buyer.user, info, '08-buyer-order-360');

    await page.goto(offer.detail);
    await button(page, 'Приостановить продажи').click();
    await expect(page.getByRole('dialog')).toContainText('Уже созданные заказы сохранятся.');
    await page.keyboard.press('Escape');
    await expect(button(page, 'Приостановить продажи')).toBeFocused();
    await page.keyboard.press('Enter');
    await button(page, 'Приостановить').click();
    await expect(page.getByText('Продажи приостановлены.')).toBeVisible();
    await operator.user.goto('/app/orders');
    const queue = operator.user.getByRole('region', { name: 'Список заказов' });
    await expect(queue.getByRole('article')).toHaveCount(1);
    expect(await queue.getByRole('article').getAttribute('data-order-id')).toBe(buyerOrder.split('/').at(-1));
    await queue.getByRole('link', { name: /Заказ/ }).click();
    await button(operator.user, 'Принять').click();
    await expect(operator.user.locator('.tag')).toHaveText('Принят');
    await expect(button(operator.user, 'Принять')).toHaveCount(0);
    await button(operator.user, 'Отметить исполненным').click();
    await expect(operator.user.locator('.tag')).toHaveText('Исполнен');
    await expect(button(operator.user, 'Отменить')).toHaveCount(0);
    await noOverflow(operator.user);
    await evidence(operator.user, info, '09-operator-order-768');
    await buyer.user.goto(buyerOrder);
    await expect(buyer.user.locator('.tag')).toHaveText('Исполнен');
    await expect(button(buyer.user, 'Отменить')).toHaveCount(0);
    const observed = await buyer.user.request.get('/api/v2/orders/' + buyerOrder.split('/').at(-1));
    expect(observed.status()).toBe(200);
    const { data, meta }: { data: DTO<'Order'>; meta: DTO<'Meta'> } = await observed.json();
    const record = JSON.stringify({
      scenario: 'CORE-01', seed: 'CORE-001', server_now: meta.server_now,
      offer_id: data.offer_id, offer_version_no: data.offer_version_no,
      quote_id: data.quote_id, reservation_id: data.reservation_id, order_id: data.id,
      subtotal_minor: data.subtotal_minor, fulfillment_fee_minor: data.fulfillment_fee_minor, total_minor: data.total_minor,
      observed_order_statuses: ['CREATED', 'ACCEPTED', data.status], order_kind: data.order_kind, is_test: data.is_test,
      paid_at: data.paid_at, native_vk_order_id: data.native_vk_order_id,
      confirm_attempts: attempts.length, exact_confirm_replay: attempts[0]?.key === attempts[1]?.key && attempts[0]?.body === attempts[1]?.body,
    }, null, 2) + '\n';
    await info.attach('core-order-chain.json', { body: record, contentType: 'application/json' });
    if (process.env.CASECHAMP_CHECKOUT_REPORT) await writeFile(process.env.CASECHAMP_CHECKOUT_REPORT, record);

    await buyer.user.goto('/buy/orders/329288cf-a024-43c8-9e8c-71ede1bf5451');
    await expect(heading(buyer.user, 'Эта страница недоступна в текущей демонстрации')).toBeVisible();
    await page.goto('/app/demo');
    await button(page, 'Сбросить демонстрацию').click();
    await expect(page.getByRole('dialog')).toContainText('Восстановится контрольный каталог.');
    await button(page, 'Сбросить').click();
    await expect(heading(page, 'Рабочий стол')).toBeVisible();
    await identity(page, 'Владелец');
    await expect(page.getByText(/Поколение 2/)).toBeVisible();
    await buyer.user.goto(buyerOrder);
    await expect(heading(buyer.user, 'Вход в демонстрацию')).toBeVisible();
    await expect(buyer.user.getByText('Демонстрация · тестовые заказы')).toBeVisible();
    await operator.user.reload();
    await expect(heading(operator.user, 'Вход в демонстрацию')).toBeVisible();
    await page.goto(offer.detail);
    await expect(heading(page, 'Эта страница недоступна в текущей демонстрации')).toBeVisible();
  } finally {
    await buyer.context.close();
    await operator.context.close();
  }
});
