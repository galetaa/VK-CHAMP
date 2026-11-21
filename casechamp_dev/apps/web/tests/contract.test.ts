import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, prepare, read, send, UnknownResult } from '../src/app/api';
import { count, integer, kop, money, sameOrigin } from '../src/app/format';

afterEach(() => vi.unstubAllGlobals());

describe('точные пользовательские значения', () => {
  it.each([
    [' 1290,03 ', 129003], ['1.2', 120], ['0,01', 1], ['1000000.00', 100000000],
    ['', null], ['0', null], ['0,001', null], ['-3', null], ['+3', null], ['1e3', null],
    ['NaN', null], ['Infinity', null], ['1000000,01', null], ['12,3.4', null],
  ])('преобразует %j', (input, result) => expect(kop(input)).toBe(result));

  it('обрабатывает целые количества и нормализованные символы', () => {
    expect(integer(' 99 ', 1, 99)).toBe(99);
    for (const input of ['', '1.0', '2,5', '1e1', '0', '100']) expect(integer(input, 1, 99)).toBeNull();
    expect(count('е\u0308😀')).toBe(2);
    expect(money(129000)).toBe('1 290,00 ₽');
  });

  it('проверяет origin и маршрут URL', () => {
    const origin = 'http://localhost:18081';
    expect(sameOrigin('/buy/id', /^\/buy\//, origin)?.href).toBe(origin + '/buy/id');
    expect(sameOrigin('//example.com/buy/id', /^\/buy\//, origin)).toBeNull();
    expect(sameOrigin('/app', /^\/buy\//, origin)).toBeNull();
  });
});

const meta = { server_now: '2026-11-20T09:00:00.000Z', request_id: 'c5dcaeaa-7126-45de-8e51-6dc5be4c9c7e', replayed: false };
const details = { fields: [], current_revision: null, current_state: null, available_quantity: null, retry_after_seconds: null };
const failure = (code: string) => ({ error: { code, message: 'Ошибка сценария', details }, meta });

describe('единый API-клиент', () => {
  it('применяет контрактный prefix и сохраняет точное тело и ключ повторной команды', async () => {
    const fetcher = vi.fn().mockImplementation(() => Promise.resolve(Response.json(failure('REVISION_CONFLICT'), { status: 409 })));
    vi.stubGlobal('fetch', fetcher);
    const intent = prepare('updateSku', { path: { sku: 'MUG' }, body: {
      title: 'Кружка', price_minor: 129000, stock_on_hand: 10, active: true, ready_in_days: 1, expected_revision: 2,
    } });
    await expect(send(intent, 'local-csrf')).rejects.toBeInstanceOf(ApiError);
    await expect(send(intent, 'local-csrf')).rejects.toMatchObject({ code: 'REVISION_CONFLICT' });
    expect(intent.url).toBe('/api/v2/catalog/skus/MUG');
    expect(intent.key).toMatch(/^[0-9a-f-]{36}$/);
    expect(fetcher.mock.calls[0]![1].body).toBe(fetcher.mock.calls[1]![1].body);
    expect(fetcher.mock.calls[0]![1].headers).toEqual({
      Accept: 'application/json', 'Content-Type': 'application/json',
      'X-CSRF-Token': 'local-csrf', 'Idempotency-Key': intent.key,
    });
  });

  it('повторяет временный 503 два раза и сразу прекращает на 429', async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn()
      .mockImplementationOnce(() => Promise.resolve(Response.json(failure('INTERNAL_ERROR'), { status: 503 })))
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockImplementation(() => Promise.resolve(Response.json(failure('RATE_LIMITED'), { status: 429 })));
    vi.stubGlobal('fetch', fetcher);
    try {
      const result = expect(read('getSession', {})).rejects.toMatchObject({ status: 429, code: 'RATE_LIMITED' });
      await vi.advanceTimersByTimeAsync(1299);
      expect(fetcher).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1);
      await result;
      expect(fetcher).toHaveBeenCalledTimes(3);
    } finally { vi.useRealTimers(); }
  });

  it('отклоняет неверный payload до выдачи результата', async () => {
    vi.stubGlobal('fetch', () => Promise.resolve(Response.json({ data: { role: 'OWNER' }, meta })));
    await expect(send(prepare('getSession', {}))).rejects.toBeInstanceOf(UnknownResult);
  });
});
