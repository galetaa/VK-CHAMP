import { expect, test } from '@playwright/test';
import { button, evidence, heading, join, publish, start, total } from './helpers';

test('B001 · предпочтительный комплект, компромисс, NO_SOLUTION, 503 и заказ', async ({ page, browser }, info) => {
  await start(page, true);
  const offer = await publish(page, 'Сервировка на четверых', 'BUNDLE');
  const buyer = await join(page, browser, 'покупателя');
  try {
    await buyer.user.goto(offer.share);
    await expect(heading(buyer.user, 'Сервировка на четверых')).toBeVisible();
    await button(buyer.user, 'Подобрать комплект').click();
    await total(buyer.user, '4 400,00 ₽');
    await expect(buyer.user.getByText('Скатерть уже есть', { exact: true }).last()).toBeVisible();
    await expect(buyer.user.getByText(/покрытие.*излишек/).first()).toBeVisible();
    await evidence(buyer.user, info, '10-bundle-solved-1440');

    const budget = buyer.user.getByLabel('Бюджет в рублях (обязательно)');
    await budget.fill('4000');
    await expect(button(buyer.user, 'Проверить условия')).toHaveCount(0);
    await button(buyer.user, 'Подобрать комплект').click();
    await total(buyer.user, '3 800,00 ₽');
    await budget.fill('3279,99');
    await button(buyer.user, 'Подобрать комплект').click();
    await expect(buyer.user.getByText('При этих условиях комплект не найден.')).toBeVisible();
    await expect(budget).toHaveValue('3279,99');
    await expect(button(buyer.user, 'Проверить условия')).toHaveCount(0);
    await evidence(buyer.user, info, '11-bundle-unavailable-1440');

    const pattern = '**/api/v2/bundle-solutions';
    await buyer.user.route(pattern, async route => {
      await route.fulfill({ status: 503, json: {
        error: {
          code: 'COMPUTATION_UNAVAILABLE', message: 'Расчёт недоступен',
          details: { fields: [], current_revision: null, current_state: null, available_quantity: null, retry_after_seconds: null },
        },
        meta: { request_id: 'c5dcaeaa-7126-45de-8e51-6dc5be4c9c7e', server_now: new Date().toISOString(), replayed: false },
      } });
    }, { times: 1 });
    await budget.fill('5000,00');
    await button(buyer.user, 'Подобрать комплект').click();
    await expect(buyer.user.getByRole('alert')).toContainText('Сейчас не удалось завершить действие.');
    await expect(buyer.user.getByText('При этих условиях комплект не найден.')).toHaveCount(0);
    await expect(button(buyer.user, 'Подобрать комплект')).toBeEnabled();
    expect(await budget.inputValue()).toBe('5000,00');
    await button(buyer.user, 'Подобрать комплект').click();
    await total(buyer.user, '4 400,00 ₽');
    const request = buyer.user.waitForRequest(req => req.url().endsWith('/api/v2/quotes') && req.method() === 'POST');
    await button(buyer.user, 'Проверить условия').click();
    const body = (await request).postDataJSON();
    expect(Object.keys(body).sort()).toEqual(['kind', 'solution_id']);
    expect(body.kind).toBe('BUNDLE');
    await total(buyer.user, '4 400,00 ₽');
    await button(buyer.user, 'Зарезервировать на 5 минут').click();
    await expect(heading(buyer.user, 'Резерв товаров')).toBeVisible();
    await button(buyer.user, 'Подтвердить тестовый заказ').click();
    await expect(heading(buyer.user, 'Тестовый заказ приложения')).toBeVisible();
    await total(buyer.user, '4 400,00 ₽');
  } finally { await buyer.context.close(); }
});
