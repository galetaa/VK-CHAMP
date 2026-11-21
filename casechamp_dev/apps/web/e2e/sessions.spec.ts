import { expect, test } from '@playwright/test';
import { button, heading, identity, join, link, publish, start } from './helpers';

test('UI-01/20 · одноразовая ссылка и восстановление сброса после перезагрузки', async ({ page, browser }) => {
  await start(page);
  await page.goto('/app/demo');
  await button(page, 'Создать ссылку покупателя').click();
  const field = page.getByRole('textbox', { name: 'Готовая ссылка' });
  await expect(field).toBeVisible();
  const invite = await field.inputValue();
  await page.goto(invite).catch(() => { throw new Error('Не удалось открыть приглашение'); });
  await button(page, 'Войти по ссылке').click();
  await expect(page.getByRole('alert')).toContainText('В этом окне уже открыта другая роль.');
  await identity(page, 'Владелец');
  expect(new URL(page.url()).hash).toBe('');
  const guest = await browser.newContext({ baseURL: new URL(invite).origin });
  const duplicate = await browser.newContext({ baseURL: new URL(invite).origin });
  try {
    const user = await guest.newPage();
    await user.goto(invite).catch(() => { throw new Error('Не удалось открыть приглашение'); });
    await button(user, 'Войти по ссылке').click();
    await identity(user, 'Покупатель');
    const retry = await duplicate.newPage();
    await retry.goto(invite).catch(() => { throw new Error('Не удалось открыть приглашение'); });
    await button(retry, 'Войти по ссылке').click();
    await expect(retry.getByRole('alert')).toContainText('Ссылка больше не действует.');
  } finally {
    await guest.close();
    await duplicate.close();
  }

  await page.goto('/app/demo');
  const attempts: { key?: string; body: string | null }[] = [];
  await page.route('**/api/v2/demo/reset', async route => {
    attempts.push({ key: route.request().headers()['idempotency-key'], body: route.request().postData() });
    const answer = await route.fetch();
    expect(answer.status()).toBe(200);
    if (attempts.length === 1) await route.abort('connectionreset');
    else {
      expect((await answer.json()).meta.replayed).toBe(true);
      await route.fulfill({ response: answer });
    }
  });
  await button(page, 'Сбросить демонстрацию').click();
  await button(page, 'Сбросить').click();
  await expect(button(page, 'Проверить выполнение')).toBeEnabled();
  await page.reload();
  await expect(button(page, 'Проверить выполнение')).toBeEnabled();
  await button(page, 'Проверить выполнение').click();
  await expect(heading(page, 'Рабочий стол')).toBeVisible();
  await expect(page.getByText(/Поколение 2/)).toBeVisible();
  expect(attempts).toHaveLength(2);
  expect(attempts[0]).toEqual(attempts[1]);
});

test('UI-12/20 · запоздалый расчёт и очистка ожидания при смене покупателя', async ({ page, browser }) => {
  await start(page);
  const offer = await publish(page, 'Поздний ответ');
  const buyer = await join(page, browser, 'покупателя');
  try {
    let arrived!: () => void;
    let continueReply!: () => void;
    const serverAnswered = new Promise<void>(resolve => { arrived = resolve; });
    const release = new Promise<void>(resolve => { continueReply = resolve; });
    await buyer.user.route('**/api/v2/quotes', async route => {
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      arrived();
      await release;
      await route.fulfill({ response });
    }, { times: 1 });
    await buyer.user.goto(offer.share);
    await button(buyer.user, 'Проверить условия').click();
    await serverAnswered;
    await expect(buyer.user.getByText('Проверяем результат действия')).toBeVisible();
    await link(buyer.user, 'К моим заказам').click();
    continueReply();
    await expect(buyer.user.getByText('Проверяем результат действия')).toHaveCount(0);
    await expect(heading(buyer.user, 'Мои тестовые заказы')).toBeVisible();

    await buyer.user.route('**/api/v2/quotes', route => route.abort('connectionreset'), { times: 1 });
    await buyer.user.goto(offer.share);
    await button(buyer.user, 'Проверить условия').click();
    await expect(button(buyer.user, 'Проверить выполнение')).toBeEnabled();
    expect(await buyer.user.evaluate(() => !!sessionStorage.getItem('casechamp-v2-pending'))).toBe(true);
    await buyer.context.clearCookies();
    await buyer.user.reload();
    await expect(heading(buyer.user, 'Вход в демонстрацию')).toBeVisible();
    expect(await buyer.user.evaluate(() => sessionStorage.getItem('casechamp-v2-pending'))).toBeNull();

    await page.goto('/app/demo');
    await button(page, 'Создать ссылку покупателя').click();
    const field = page.getByRole('textbox', { name: 'Готовая ссылка' });
    await expect(field).toBeVisible();
    const fresh = await field.inputValue();
    await buyer.user.goto(fresh).catch(() => { throw new Error('Не удалось открыть приглашение'); });
    await button(buyer.user, 'Войти по ссылке').click();
    await identity(buyer.user, 'Покупатель');
    await buyer.user.goto(offer.share);
    await expect(heading(buyer.user, 'Поздний ответ')).toBeVisible();
    await expect(button(buyer.user, 'Проверить выполнение')).toHaveCount(0);
  } finally { await buyer.context.close(); }
});
