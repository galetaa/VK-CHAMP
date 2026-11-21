import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError } from '../app/api';
import type { DTO } from '../app/api';
import { integer, label, money } from '../app/format';
import { useScreenNavigation } from '../app/navigation';
import { useCountdown, useResource } from '../app/resource';
import { reservationStorageKey, scopeOf, useSession } from '../app/session';
import { Busy, ConfirmDialog, Empty, errorsOf, Field, focusError, Inaccessible, Lines, Problem, Tag, Timer, Title, Total } from '../ui/common';
import { BundleCheckout, FulfillmentFields, initialMethod, methodCaption } from './bundles';
import { Template } from './merchantOffers';

type PublicOffer = DTO<'PublicOffer'>;
type Quote = DTO<'Quote'>;
type Reservation = DTO<'Reservation'>;
const linkOffer = (id: string) => '/buy/' + id;
const unavailable = (error: unknown) => error instanceof ApiError && [403, 404].includes(error.status);

export function BuyerOffer() {
  const id = useParams().id!;
  const resource = useResource('getBuyerOffer', { path: { offer_id: id } });
  const { event } = useSession();
  const version = resource.data?.version.version_no;
  useEffect(() => {
    if (version) event('offer_view', { offerId: id, version });
  }, [id, version, event]);
  if (unavailable(resource.error)) return <Inaccessible />;
  if (resource.error instanceof ApiError && resource.error.code === 'OFFER_UNAVAILABLE') return <>
    <Title title="Предложение недоступно" /><Empty title="Продажи по этой ссылке остановлены"
      action={<Link className="button secondary" to="/">К моим заказам</Link>} /></>;
  if (!resource.data) return resource.error
    ? <Problem error={resource.error} retry={resource.reload} /> : <Busy text="Открываем предложение…" />;
  const offer = resource.data;
  const published = offer.version;
  return <>
    <Title title={published.title} description={'Продавец · Магазин демонстрации · версия ' + published.version_no}
      back={{ to: '/', text: 'К моим заказам' }} />
    {offer.needs_review && <div className="notice issue" role="status"><b>Продавцу нужно обновить условия предложения.</b></div>}
    <div className="split">
      <section className="panel stack align-start">
        <h2>{offer.mode === 'FIXED' ? 'Состав одного предложения' : 'Шаблон комплекта'}</h2>
        {published.description && <p className="multiline">{published.description}</p>}
        {offer.mode === 'FIXED' ? <>
          <Lines lines={published.lines} />
          <ul className="coverage">{published.lines.map(line => <li key={line.sku}>
            <b>{line.sku}</b> · Доступно {offer.availability.find(row => row.sku === line.sku)?.available_quantity ?? 0}
            {' · Готовность '}{published.catalog_terms.find(row => row.sku === line.sku)?.ready_in_days ?? '—'} дн.
          </li>)}</ul>
          <div className="amount-label">Товары в одном предложении
            <strong>{money(published.lines.reduce((sum, line) => sum + line.line_total_minor, 0))}</strong></div>
        </> : published.bundle_template && <Template template={published.bundle_template} />}
        <div className="details"><span>Способы получения <b>{published.fulfillment_methods.map(methodCaption).join(' / ')}</b></span></div>
      </section>
      {offer.mode === 'FIXED'
        ? <FixedCheckout key={id + ':' + version} offer={offer} refresh={resource.reload} />
        : <BundleCheckout key={id + ':' + version} offer={offer} refresh={resource.reload} />}
    </div>
  </>;
}

function FixedCheckout({ offer, refresh }: { offer: PublicOffer; refresh: () => void }) {
  const navigate = useScreenNavigation();
  const { mutate, busy, pending, event } = useSession();
  const { version } = offer;
  const [copies, setCopies] = useState('1');
  const [days, setDays] = useState('1');
  const [fulfillment, setMethod] = useState(() => initialMethod(version.fulfillment_methods));
  const [touched, mark] = useState({ copies: false, days: false });
  const [error, setError] = useState<unknown>(null);
  const form = useRef<HTMLFormElement>(null);
  const quantity = integer(copies, 1, 10);
  const term = integer(days, 1, 30);
  const units = version.lines.reduce((sum, line) => sum + line.quantity, 0);
  const copiesError = quantity === null ? 'Введите целое число от 1 до 10.' : units * quantity > 990
    ? 'Общее количество товаров не может превышать 990.' : undefined;
  const missing = quantity === null || copiesError ? undefined : version.lines.find(line =>
    line.quantity * quantity > (offer.availability.find(row => row.sku === line.sku)?.available_quantity ?? 0));
  const locked = busy || !!pending;
  const issues = errorsOf(error);

  async function quote(e: FormEvent) {
    e.preventDefault();
    if (locked || offer.needs_review || missing) return;
    mark({ copies: true, days: true });
    if (quantity === null || copiesError || term === null) { focusError(form.current); return; }
    event('cta_click', { offerId: offer.id, version: version.version_no }, 'CHECKOUT');
    setError(null);
    try {
      const result = await mutate('createQuote', { body: {
        kind: 'OFFER', offer_id: offer.id, offer_version_no: version.version_no,
        copies: quantity, fulfillment, requested_days: term,
      } });
      navigate('/buy/quotes/' + result.id);
    } catch (issue) {
      setError(issue);
      focusError(form.current);
      if (issue instanceof ApiError && [
        'INSUFFICIENT_STOCK', 'OFFER_VERSION_CHANGED', 'OFFER_NEEDS_REVIEW', 'CATALOG_CHANGED', 'OFFER_UNAVAILABLE',
      ].includes(issue.code)) refresh();
    }
  }
  return <form ref={form} className="panel summary stack align-start" noValidate onSubmit={quote}>
    <h2>Выбор покупателя</h2>
    <Problem error={error} detail={!Object.keys(issues).length} />
    {missing && <div className="notice issue" role="status"><div>
      <b>Не хватает товара для этого состава.</b><p>{missing.title} · {missing.sku}</p>
      <button type="button" className="button secondary" onClick={refresh}>Обновить наличие</button>
    </div></div>}
    <fieldset className="stack" disabled={locked}>
      <Field label="Количество одинаковых предложений (обязательно)"
        error={issues['/copies'] ?? (touched.copies ? copiesError : undefined)} hint="От 1 до 10; состав каждого предложения фиксирован">
        {props => <input {...props} inputMode="numeric" value={copies} onBlur={() => mark(old => ({ ...old, copies: true }))}
          onChange={e => { setCopies(e.target.value); setError(null); }} />}</Field>
      <FulfillmentFields methods={version.fulfillment_methods} fulfillment={fulfillment} days={days}
        changeMethod={value => { setMethod(value); setError(null); }} changeDays={value => { setDays(value); setError(null); }}
        touchDays={() => mark(old => ({ ...old, days: true }))}
        daysError={issues['/requested_days'] ?? (touched.days && term === null ? 'Введите целое число от 1 до 30.' : undefined)}
        methodError={issues['/fulfillment']} />
      <button className="button primary" disabled={offer.needs_review || !!missing}>Проверить условия</button>
    </fieldset>
  </form>;
}

export function PurchaseFacts({ data }: { data: Pick<Quote, 'fulfillment' | 'requested_days' | 'offer_version_no'> }) {
  return <div className="details">
    <span>Получение <b>{label(data.fulfillment)}</b></span>
    <span>Запрошенный срок <b>{data.requested_days} дн.</b></span>
    <span>Версия предложения <b>{data.offer_version_no}</b></span>
  </div>;
}

function reservedId(scope: string, quote: string, reservation?: string) {
  const key = reservationStorageKey;
  try {
    if (reservation) sessionStorage.setItem(key, JSON.stringify({ scope, quote, reservation }));
    const saved = JSON.parse(sessionStorage.getItem(key) ?? 'null');
    if (saved?.scope !== scope) sessionStorage.removeItem(key);
    return saved?.scope === scope && saved.quote === quote && /^[0-9a-f-]{36}$/.test(saved.reservation)
      ? saved.reservation as string : null;
  } catch { return null; }
}

export function QuotePage() {
  const id = useParams().id!;
  const resource = useResource('getQuote', { path: { quote_id: id } });
  if (unavailable(resource.error)) return <Inaccessible />;
  if (!resource.data) return resource.error
    ? <Problem error={resource.error} retry={resource.reload} /> : <Busy text="Читаем расчёт…" />;
  return <QuoteStep key={id} quote={resource.data} reload={resource.reload} />;
}

function QuoteStep({ quote, reload }: { quote: Quote; reload: () => void }) {
  const navigate = useScreenNavigation();
  const { mutate, busy, pending, session } = useSession();
  const [error, setError] = useState<unknown>(null);
  const [history, setHistory] = useState(false);
  const seconds = useCountdown(quote.expires_at, reload);
  const quotedScope = scopeOf(session!);
  const known = reservedId(quotedScope, quote.id);
  const old = history || !seconds;
  async function reserve() {
    if (old || known || busy || pending) return;
    setError(null);
    try {
      const item = await mutate('createReservation', { body: { quote_id: quote.id } });
      reservedId(quotedScope, quote.id, item.id);
      navigate('/buy/reservations/' + item.id);
    } catch (issue) {
      setError(issue);
      if (issue instanceof ApiError && [
        'CATALOG_CHANGED', 'OFFER_VERSION_CHANGED', 'OFFER_NEEDS_REVIEW', 'SKU_UNAVAILABLE',
        'INSUFFICIENT_STOCK', 'QUOTE_EXPIRED', 'QUOTE_ALREADY_USED', 'OFFER_UNAVAILABLE',
      ].includes(issue.code)) {
        setHistory(true);
        reload();
      }
    }
  }
  return <>
    <Title title="Проверка полной стоимости" description="Сверьте состав и условия перед резервированием."
      back={{ to: linkOffer(quote.offer_id), text: 'К предложению' }} />
    <Problem error={error} />
    {(old || known) && <div className="notice info" role="status">
      {known ? 'По этому расчёту уже создавался резерв.' : seconds ? 'Исходный расчёт сохранён для просмотра.' : 'Время расчёта истекло.'}
    </div>}
    <div className="split">
      <section className="panel stack"><h2>{old || known ? 'История расчёта' : 'Состав расчёта'}</h2>
        <Lines lines={quote.lines} /><PurchaseFacts data={quote} />
      </section>
      <aside className="panel stack summary"><h2>Стоимость</h2><Total total={quote} />
        {!known && <p>Товары пока не зарезервированы.</p>}
        {!known && <Timer expiry={quote.expires_at} remaining={seconds} />}
        {known && <Link className="button primary" to={'/buy/reservations/' + known}>Проверить резерв</Link>}
        {!history && !known && <button className="button primary" disabled={old || busy || !!pending} onClick={() => void reserve()}>Зарезервировать на 5 минут</button>}
        <Link className="button secondary" to={linkOffer(quote.offer_id)}>{old || known ? 'Проверить условия заново' : 'Изменить выбор'}</Link>
      </aside>
    </div>
  </>;
}

export function ReservationPage() {
  const id = useParams().id!;
  const resource = useResource('getReservation', { path: { reservation_id: id } },
    { interval: 5000, when: data => data.state === 'ACTIVE' });
  if (unavailable(resource.error)) return <Inaccessible />;
  if (!resource.data) return resource.error
    ? <Problem error={resource.error} retry={resource.reload} /> : <Busy text="Сверяем резерв…" />;
  return <ReservationStep key={id} item={resource.data} reload={resource.reload} />;
}

function ReservationStep({ item, reload }: { item: Reservation; reload: () => Promise<Reservation | undefined> }) {
  const { mutate, busy, pending, session } = useSession();
  const navigate = useScreenNavigation();
  const [error, setError] = useState<unknown>(null);
  const [cancel, setCancel] = useState(false);
  const active = item.state === 'ACTIVE';
  const seconds = useCountdown(active ? item.expires_at : undefined, reload);
  const locked = !seconds || busy || !!pending;
  useEffect(() => { if (session) reservedId(scopeOf(session), item.quote_id, item.id); }, [session, item.quote_id, item.id]);
  async function change(kind: 'confirmReservation' | 'cancelReservation') {
    if (locked || !active) return;
    setError(null);
    try {
      const input = { path: { reservation_id: item.id }, body: { expected_revision: item.revision } };
      if (kind === 'confirmReservation') {
        const result = await mutate('confirmReservation', input);
        navigate('/buy/orders/' + result.id);
      } else {
        await mutate('cancelReservation', input);
        await reload();
      }
      setCancel(false);
    } catch (issue) {
      setError(issue);
      setCancel(false);
      if (issue instanceof ApiError && issue.status === 409) {
        const latest = await reload();
        if (latest?.order_id) navigate('/buy/orders/' + latest.order_id);
      }
    }
  }
  return <>
    <Title title="Резерв товаров" description="Состав и стоимость зафиксированы в резерве."
      back={{ to: linkOffer(item.offer_id), text: 'К предложению' }} />
    <Problem error={error} retry={reload} />
    <div className="split">
      <section className="panel stack"><div className="section-heading"><h2>Состав резерва</h2><Tag value={item.state} /></div>
        <Lines lines={item.quote.lines} /><PurchaseFacts data={item.quote} />
      </section>
      <aside className="panel stack summary"><h2>Стоимость резерва</h2><Total total={item.quote} />
        {active && <>
          <Timer expiry={item.expires_at} remaining={seconds} />
          {!seconds && <div className="notice info" role="status">Проверяем состояние резерва.</div>}
          <button className="button primary" disabled={locked} onClick={() => void change('confirmReservation')}>Подтвердить тестовый заказ</button>
          <button className="button secondary" disabled={locked} onClick={() => setCancel(true)}>Отменить резерв</button>
        </>}
        {item.state === 'EXPIRED' && <div className="notice info" role="status">Время резерва истекло. Товары освобождены.</div>}
        {item.state === 'CANCELLED' && <div className="notice info" role="status">
          {item.cancel_reason === 'OFFER_UNAVAILABLE' ? 'Продавец остановил предложение. Резерв отменён.' : 'Резерв отменён.'}</div>}
        {item.order_id && <Link className="button primary" to={'/buy/orders/' + item.order_id}>Открыть тестовый заказ</Link>}
        {!active && !item.order_id && <Link className="button secondary" to={linkOffer(item.offer_id)}>Проверить условия заново</Link>}
      </aside>
    </div>
    {cancel && <ConfirmDialog title="Отменить резерв?" confirm="Отменить резерв" disabled={busy || !!pending}
      approve={() => void change('cancelReservation')} dismiss={() => setCancel(false)}>
      <p>Товары вернутся в доступный остаток. Для нового резерва потребуется новый расчёт.</p>
    </ConfirmDialog>}
  </>;
}
