import { AlertCircle, CheckCircle2, Clipboard, LoaderCircle, PackageOpen } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, UnknownResult } from '../app/api';
import type { DTO } from '../app/api';
import { date, label, money } from '../app/format';
import { useCountdown } from '../app/resource';

const explanations: Partial<Record<DTO<'ErrorEnvelope'>['error']['code'], string>> = {
  VALIDATION_ERROR: 'Проверьте отмеченные поля.',
  DRAFT_INCOMPLETE: 'Заполните условия предложения перед публикацией.',
  REVISION_CONFLICT: 'Данные изменились в другом окне.',
  OFFER_NEEDS_REVIEW: 'Продавцу нужно обновить условия предложения.',
  OFFER_VERSION_CHANGED: 'Предложение обновлено. Проверьте новый расчёт.',
  CATALOG_CHANGED: 'Условия изменились. Проверьте новый расчёт.',
  INSUFFICIENT_STOCK: 'Не хватает товара для этого состава.',
  SKU_UNAVAILABLE: 'Товар сейчас недоступен или не подходит по сроку.',
  QUOTE_EXPIRED: 'Время расчёта истекло.',
  QUOTE_ALREADY_USED: 'По этому расчёту уже создавался резерв.',
  RESERVATION_EXPIRED: 'Время резерва истекло.',
  RESERVATION_STATE_CONFLICT: 'Резерв уже обработан.',
  ORDER_STATE_CONFLICT: 'Заказ уже обработан.',
  OFFER_UNAVAILABLE: 'Предложение больше недоступно.',
  STOCK_BELOW_RESERVED: 'Часть товара уже зарезервирована.',
  STOCK_CAPACITY_CONFLICT: 'Значение слишком велико с учётом возможного возврата из незавершённых заказов.',
  AUTH_REQUIRED: 'Сессия завершилась. Откройте демонстрацию или новую ссылку роли.',
  SESSION_STALE: 'Сессия завершилась после сброса. Получите новую ссылку роли.',
  SANDBOX_EXPIRED: 'Время демонстрации истекло.',
  CSRF_REJECTED: 'Не удалось проверить действие. Сессия обновляется.',
  RATE_LIMITED: 'Слишком много запросов.',
  BUNDLE_SOLUTION_EXPIRED: 'Расчёт комплекта нужно обновить.',
  BUNDLE_SOLUTION_STALE: 'Расчёт комплекта нужно обновить.',
  BUNDLE_NOT_SOLVED: 'Этот расчёт не содержит допустимого комплекта.',
  MODULE_DISABLED: 'Подбор не включён в этой демонстрации.',
  COMPUTATION_UNAVAILABLE: 'Сейчас не удалось завершить действие.',
  INTERNAL_ERROR: 'Сейчас не удалось завершить действие.',
  IDEMPOTENCY_CONFLICT: 'Запрос отличается от исходного. Сверьте состояние ресурса.',
  SESSION_ALREADY_ACTIVE: 'В этом окне уже открыта другая роль. Откройте ссылку в отдельном профиле или приватном окне.',
  ROLE_LINK_EXPIRED: 'Ссылка больше не действует. Владелец демонстрации может создать новую.',
  ROLE_LINK_CONSUMED: 'Ссылка больше не действует. Владелец демонстрации может создать новую.',
};

export function Title({ title, description, action, back }: {
  title: string; description?: string; action?: ReactNode; back?: { to: string; text: string };
}) {
  return <header className="page-title">
    <div>{back && <Link to={back.to} className="back">{back.text}</Link>}
      <h1 tabIndex={-1} id="page-title">{title}</h1>{description && <p>{description}</p>}</div>
    {action && <div className="title-action">{action}</div>}
  </header>;
}

export function Tag({ value }: { value: string }) {
  const tone = ['ACTIVE', 'FULFILLED', 'CONFIRMED'].includes(value) ? 'good'
    : ['PAUSED', 'ACCEPTED'].includes(value) ? 'warm'
      : ['CANCELLED', 'ARCHIVED', 'EXPIRED'].includes(value) ? 'quiet' : 'blue';
  return <span className={'tag ' + tone}><i />{label(value)}</span>;
}

export function Busy({ text = 'Загрузка…' }: { text?: string }) {
  return <div className="panel blank" role="status" aria-busy="true"><LoaderCircle className="spin" size={22} />{text}</div>;
}
export function Empty({ title, text, action }: { title: string; text?: string; action?: ReactNode }) {
  return <div className="panel empty">
    <span className="large-icon"><PackageOpen size={24} /></span><h2>{title}</h2>
    {text && <p>{text}</p>}{action}
  </div>;
}
export const Inaccessible = () => <>
  <Title title="Эта страница недоступна в текущей демонстрации" />
  <Link className="button secondary" to="/">К началу</Link>
</>;

export function Problem({ error, retry, inLink = false, detail = true, explanation }: {
  error: unknown; retry?: () => void; inLink?: boolean; detail?: boolean; explanation?: string;
}) {
  if (!error) return null;
  const api = error instanceof ApiError ? error : null;
  let text = api ? explanations[api.code] ?? api.message : error instanceof UnknownResult
    ? error.kind === 'schema' ? 'Ответ имеет неожиданный формат. Сверьте состояние перед новым действием.'
      : 'Не удалось получить ответ сервера.' : error instanceof Error ? error.message : 'Действие не выполнено.';
  if (api?.status === 404) text = inLink
    ? 'Ссылка больше не действует. Владелец демонстрации может создать новую.'
    : 'Эта страница недоступна в текущей демонстрации';
  if (api?.code === 'FORBIDDEN') text = 'Это действие недоступно текущей роли.';
  const id = api?.payload.meta.request_id ?? (error instanceof UnknownResult ? error.requestId : undefined);
  return <div className="notice issue" role="alert">
    <AlertCircle size={20} aria-hidden="true" /><div>
      <strong>{explanation ?? text}</strong>
      {detail && !!api?.details.fields.length && <ul>{api.details.fields.map(item =>
        <li key={item.path + item.code}>{humanField(item.path)} {item.message}</li>)}</ul>}
      {api?.details.retry_after_seconds && <p>Повторите через {api.details.retry_after_seconds} сек.</p>}
      {api?.details.available_quantity !== null && api?.details.available_quantity !== undefined &&
        <p>Доступно {api.details.available_quantity}.</p>}
      {id && <small>Номер запроса {id}</small>}
      {retry && <div><button type="button" className="button secondary small" onClick={retry}>Повторить чтение</button></div>}
    </div>
  </div>;
}

const fields: Record<string, string> = {
  title: 'Название.', description: 'Описание.', source_text: 'Источник.', lines: 'Состав.',
  fulfillment_methods: 'Получение.', stock_on_hand: 'Остаток.', ready_in_days: 'Готовность.',
  price_minor: 'Цена.', units_per_pack: 'Единиц в упаковке.', requested_days: 'Срок.',
  copies: 'Число копий.', budget_minor: 'Бюджет.', guests: 'Гости.', sku: 'SKU.',
  bundle_template_id: 'Шаблон.', cancel_reason: 'Причина отмены.',
};
export const humanField = (path: string) => fields[path.split('/').at(-1) ?? ''] ?? 'Параметры запроса.';
export const errorsOf = (error: unknown) => error instanceof ApiError
  ? Object.fromEntries(error.details.fields.map(x => [x.path, x.message])) : {};

export function Field({ label, error, hint, children, id }: {
  label: string; error?: string; hint?: string; children: (props: {
    id: string; 'aria-invalid': boolean; 'aria-describedby'?: string;
  }) => ReactNode; id?: string;
}) {
  const generated = useId();
  const identity = id ?? generated;
  return <div className="field"><label htmlFor={identity}>{label}</label>
    {children({ id: identity, 'aria-invalid': !!error, 'aria-describedby': hint || error ? identity + '-hint' : undefined })}
    {(hint || error) && <span className={error ? 'field-error' : 'hint'} id={identity + '-hint'}>{error || hint}</span>}
  </div>;
}

export function focusError(form: HTMLFormElement | null) {
  requestAnimationFrame(() => form?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus());
}

export function Copy({ value, caption = 'Скопировать ссылку' }: { value: string; caption?: string }) {
  const [copied, setCopied] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  return <div className="copy-field">
    <input ref={input} aria-label="Готовая ссылка" value={value} readOnly />
    <button type="button" className="button secondary" onClick={async () => {
      try { await navigator.clipboard.writeText(value); setCopied(true); }
      catch { input.current?.focus(); input.current?.select(); }
    }}><Clipboard size={17} />{copied ? 'Скопировано' : caption}</button>
    <span className="sr-only" role="status">{copied ? 'Ссылка скопирована' : ''}</span>
  </div>;
}

export function ConfirmDialog({ title, children, confirm, disabled, invalid, approve, dismiss }: {
  title: string; children: ReactNode; confirm: string; disabled?: boolean; invalid?: boolean;
  approve: () => void; dismiss: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const node = dialog.current;
    const caller = document.activeElement as HTMLElement | null;
    node?.showModal();
    return () => {
      node?.close();
      caller?.focus();
    };
  }, []);
  const submit = (event: FormEvent) => { event.preventDefault(); if (!disabled && !invalid) approve(); };
  return <dialog ref={dialog} aria-labelledby={id} onCancel={(event) => {
    event.preventDefault(); if (!disabled) dismiss();
  }}>
    <form onSubmit={submit}><h2 id={id}>{title}</h2>
      {children}
      <div className="actions"><button type="button" className="button secondary" autoFocus disabled={disabled}
        onClick={dismiss}>Назад</button>
      <button type="submit" className="button danger" disabled={disabled || invalid}>{confirm}</button></div>
    </form>
  </dialog>;
}

export function Timer({ expiry, remaining }: { expiry: string; remaining: number }) {
  const seconds = String(remaining % 60).padStart(2, '0');
  const minutes = String(Math.floor(remaining / 60)).padStart(2, '0');
  return <div className="time-box">
    <strong aria-hidden="true" data-testid="time-left">{minutes}:{seconds}</strong>
    <div><b>Действует до</b><span>{date(expiry)}</span></div>
    <span className="sr-only" role="status">{remaining === 0 ? 'Время истекло' : remaining <= 60 ? 'Осталась одна минута или меньше' : ''}</span>
  </div>;
}

export function Countdown({ expiry, expire }: { expiry: string; expire?: () => void }) {
  const seconds = useCountdown(expiry, expire);
  return <Timer expiry={expiry} remaining={seconds} />;
}

type Line = Pick<DTO<'QuoteLine'>, 'sku' | 'title' | 'quantity' | 'unit_price_minor' | 'line_total_minor'>;
export function Lines({ lines, unit = 'шт.' }: { lines: Line[]; unit?: string }) {
  return <ul className="line-list">{lines.map(item => <li key={item.sku}>
    <div className="line-label"><b>{item.title}</b><small>{item.sku}</small></div>
    <span>{item.quantity} {unit} × <span className="unit-amount">{money(item.unit_price_minor)}</span></span>
    <strong>{money(item.line_total_minor)}</strong>
  </li>)}</ul>;
}

export function Total({ total }: { total: { subtotal_minor: number; fulfillment_fee_minor: number; total_minor: number } }) {
  return <dl className="totals"><div><dt>Товары</dt><dd>{money(total.subtotal_minor)}</dd></div>
    <div><dt>Получение</dt><dd>{money(total.fulfillment_fee_minor)}</dd></div>
    <div className="total-final"><dt>Итого</dt><dd data-testid="total">{money(total.total_minor)}</dd></div>
  </dl>;
}

export const Success = ({ children }: { children: ReactNode }) =>
  <div className="notice success" role="status"><CheckCircle2 size={20} /> <div>{children}</div></div>;
