const rubles = new Intl.NumberFormat('ru-RU', { useGrouping: true });
const stamp = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow', dateStyle: 'medium', timeStyle: 'medium',
});
export const money = (amount: number) => rubles.format(Math.floor(amount / 100))
  + ',' + String(amount % 100).padStart(2, '0') + ' ₽';
export const date = (value: string) => stamp.format(new Date(value)) + ' МСК';
export const decimal = (amount: number) => (amount / 100).toFixed(2).replace('.', ',');
export const clean = (value: string) => value.normalize('NFC');
export const count = (value: string) => Array.from(value.normalize('NFC')).length;
export function integer(value: string, minimum: number, maximum: number) {
  const input = value.trim();
  if (!/^\d+$/.test(input)) return null;
  const n = Number(input);
  return Number.isSafeInteger(n) && n >= minimum && n <= maximum ? n : null;
}
export function kop(value: string) {
  const match = /^(\d+)(?:[.,](\d{1,2}))?$/.exec(value.trim());
  if (!match) return null;
  const subtotal = Number(match[1]) * 100 + Number((match[2] ?? '').padEnd(2, '0'));
  return Number.isSafeInteger(subtotal) && subtotal >= 1 && subtotal <= 100_000_000 ? subtotal : null;
}
export function sameOrigin(value: string, pattern: RegExp, origin: string = window.location.origin) {
  try {
    const url = new URL(value, origin);
    return url.origin === origin && !url.username && !url.password && pattern.test(url.pathname) ? url : null;
  } catch {
    return null;
  }
}
export const names: Record<string, string> = {
  OWNER: 'Владелец', OPERATOR: 'Оператор', BUYER: 'Покупатель',
  DRAFT: 'Черновик', ACTIVE: 'Активно', PAUSED: 'Приостановлено', ARCHIVED: 'В архиве',
  CREATED: 'Создан', ACCEPTED: 'Принят', FULFILLED: 'Исполнен', CANCELLED: 'Отменён',
  EXPIRED: 'Истёк', CONFIRMED: 'Подтверждён', FIXED: 'Фиксированный состав', BUNDLE: 'Подбор комплекта',
  PICKUP: 'Самовывоз', COURIER_ZONE_A: 'Курьер · зона A',
  POST: 'Из публикации', MANUAL: 'Вручную', CONSULTATION: 'После консультации',
  CUSTOMER_REQUEST: 'По просьбе покупателя', CANNOT_FULFILL: 'Невозможно исполнить',
  DEMO_CORRECTION: 'Исправление демонстрации', PLATE: 'Тарелки', GLASS: 'Стаканы',
  NAPKIN: 'Салфетки', TABLECLOTH: 'Скатерть',
};
export const label = (key: string) => names[key] ?? key;
