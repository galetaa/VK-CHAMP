import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ApiError } from '../app/api';
import type { DTO } from '../app/api';
import { decimal, integer, kop, label, money } from '../app/format';
import { useCountdown, useResource } from '../app/resource';
import { useSession } from '../app/session';
import { useScreenNavigation } from '../app/navigation';
import { Busy, errorsOf, Field, focusError, Lines, Problem, Timer, Total } from '../ui/common';

type PublicOffer = DTO<'PublicOffer'>;
type Params = DTO<'BundleSolveRequest'>;
type Result = DTO<'BundleResult'>;
export type Fulfillment = Params['fulfillment'];
export const initialMethod = (methods: Fulfillment[]) => methods.includes('PICKUP') ? 'PICKUP' : methods[0]!;
export const methodCaption = (method: Fulfillment) => label(method) + ' · ' + money(method === 'PICKUP' ? 0 : 30_000);

export function FulfillmentFields({ methods, fulfillment, days, changeMethod, changeDays, daysError, methodError, touchDays }: {
  methods: Fulfillment[]; fulfillment: Fulfillment; days: string; daysError?: string; methodError?: string;
  changeMethod: (value: Fulfillment) => void; changeDays: (value: string) => void; touchDays: () => void;
}) {
  return <>
    <Field label="Способ получения (обязательно)" error={methodError}>{props => <select {...props} value={fulfillment}
      onChange={e => changeMethod(e.target.value as Fulfillment)}>
      {methods.map(value => <option key={value} value={value}>{methodCaption(value)}</option>)}</select>}</Field>
    <Field label="Получение не позднее чем через указанное число дней" error={daysError} hint="От 1 до 30 дней">
      {props => <input {...props} inputMode="numeric" value={days} onBlur={touchDays} onChange={e => changeDays(e.target.value)} />}</Field>
  </>;
}

export function BundleCheckout({ offer, refresh }: { offer: PublicOffer; refresh: () => void }) {
  const navigate = useScreenNavigation();
  const [query, setQuery] = useSearchParams();
  const id = query.get('solution');
  const solution = useResource('getBundleSolution', { path: { solution_id: id ?? '' } }, { enabled: !!id });
  const { mutate, busy, pending, event, session } = useSession();
  const { version } = offer;
  const template = version.bundle_template;
  const [guests, setGuests] = useState('4');
  const [budget, setBudget] = useState('5000,00');
  const [owned, setOwned] = useState(true);
  const [days, setDays] = useState('1');
  const [fulfillment, setFulfillment] = useState(() => initialMethod(version.fulfillment_methods));
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const form = useRef<HTMLFormElement>(null);
  const applied = useRef<string | null>(null);
  const sequence = useRef(0);
  const valid = session?.features.bundle && template;
  const locked = busy || !!pending;
  const guestNumber = integer(guests, 1, 12);
  const budgetNumber = kop(budget);
  const dayNumber = integer(days, 1, 30);
  const values: Params | null = guestNumber === null || budgetNumber === null || dayNumber === null ? null : {
    offer_id: offer.id, offer_version_no: version.version_no, guests: guestNumber, budget_minor: budgetNumber,
    owned: owned ? ['TABLECLOTH'] : [], fulfillment, requested_days: dayNumber,
  };
  const visible = result && values &&
    (['offer_id', 'offer_version_no', 'guests', 'budget_minor', 'fulfillment', 'requested_days'] as const)
      .every(key => result.request[key] === values[key]) && result.request.owned.length === values.owned.length ? result : null;
  const seconds = useCountdown(visible?.expires_at, id ? solution.reload : undefined);

  useEffect(() => {
    const data = solution.data;
    if (!id || !data || applied.current === id || data.offer_id !== offer.id ||
      data.offer_version_no !== version.version_no || data.buyer_id !== session?.actor_id) return;
    applied.current = id;
    setResult(data);
    setGuests(String(data.request.guests));
    setBudget(decimal(data.request.budget_minor));
    setOwned(data.request.owned.includes('TABLECLOTH'));
    setDays(String(data.request.requested_days));
    setFulfillment(data.request.fulfillment);
  }, [solution.data, id, offer.id, version.version_no, session?.actor_id]);

  function edit(setter: () => void) {
    sequence.current++;
    setter();
    setResult(null);
    setError(null);
    if (id) setQuery({}, { replace: true });
  }
  function touch(name: string) { setTouched(old => ({ ...old, [name]: true })); }

  async function solve(e: FormEvent) {
    e.preventDefault();
    if (!valid || locked || offer.needs_review) return;
    setTouched({ guests: true, budget: true, days: true });
    if (!values) { focusError(form.current); return; }
    const turn = ++sequence.current;
    setError(null);
    setResult(null);
    if (id) setQuery({}, { replace: true });
    event('cta_click', { offerId: offer.id, version: version.version_no }, 'CHECKOUT');
    try {
      const next = await mutate('solveBundle', { body: values });
      if (turn === sequence.current && next.offer_version_no === version.version_no) setResult(next);
    } catch (issue) {
      if (turn === sequence.current) { setError(issue); focusError(form.current); }
      if (issue instanceof ApiError && issue.status === 409) refresh();
    }
  }

  async function quote() {
    if (locked || !visible || visible.status !== 'SOLVED' || !seconds || offer.needs_review) return;
    setError(null);
    try {
      const result = await mutate('createQuote', { body: { kind: 'BUNDLE', solution_id: visible.id } });
      navigate('/buy/quotes/' + result.id);
    } catch (issue) {
      setError(issue);
      if (issue instanceof ApiError && issue.status === 409) {
        setResult(null);
        if (id) setQuery({}, { replace: true });
        refresh();
      }
    }
  }

  if (!valid) return <div className="notice issue">Подбор недоступен для этого предложения.</div>;
  const issues = errorsOf(error);
  return <div className="stack">
    <form className="panel stack summary" ref={form} noValidate onSubmit={solve}>
      <h2>Условия подбора</h2>
      <Problem error={error} detail={!Object.keys(issues).length} />
      <fieldset className="stack" disabled={locked}>
        <Field label="Гостей (обязательно)" error={issues['/guests'] ?? (touched.guests && guestNumber === null ? 'Введите целое число от 1 до 12.' : undefined)}>
          {props => <input {...props} inputMode="numeric" value={guests} onBlur={() => touch('guests')} onChange={e => edit(() => setGuests(e.target.value))} />}</Field>
        <Field label="Бюджет в рублях (обязательно)" error={issues['/budget_minor'] ?? (touched.budget && budgetNumber === null ? 'Введите сумму от 0,01 до 1 000 000,00 ₽.' : undefined)}>
          {props => <input {...props} inputMode="decimal" value={budget} onBlur={() => touch('budget')} onChange={e => edit(() => setBudget(e.target.value))} />}</Field>
        <label className="choice"><input type="checkbox" checked={owned} onChange={e => edit(() => setOwned(e.target.checked))} />Скатерть уже есть</label>
        <FulfillmentFields methods={version.fulfillment_methods} fulfillment={fulfillment} days={days}
          changeMethod={value => edit(() => setFulfillment(value))} changeDays={value => edit(() => setDays(value))} touchDays={() => touch('days')}
          daysError={issues['/requested_days'] ?? (touched.days && dayNumber === null ? 'Введите целое число от 1 до 30.' : undefined)}
          methodError={issues['/fulfillment']} />
        <button className="button primary" disabled={offer.needs_review}>Подобрать комплект</button>
      </fieldset>
    </form>
    {solution.loading && <Busy text="Сверяем результат подбора…" />}
    <Problem error={solution.error} retry={solution.reload} />
    {visible?.status === 'NO_SOLUTION' && <div className="notice info" role="status"><b>При этих условиях комплект не найден.</b></div>}
    {visible?.status === 'SOLVED' && <section className="panel stack">
      <h2>Подобранный комплект</h2>
      <p>Сервер сначала учитывает предпочтение образцу, затем излишек и стоимость.</p>
      <Lines lines={visible.lines} unit="уп." />
      <ul className="coverage">{template.slots.map(slot => {
        const need = slot.need_rule === 'GUESTS' ? visible.request.guests : 1;
        if (slot.owned_key && visible.request.owned.includes(slot.owned_key)) {
          return <li key={slot.slot_id}>{label(slot.slot_id)} уже есть</li>;
        }
        const packs = slot.alternatives.flatMap(variant => visible.lines.filter(line => line.sku === variant.sku)
          .map(line => line.quantity * variant.units_per_pack));
        const covered = packs.reduce((sum, units) => sum + units, 0);
        return <li key={slot.slot_id}>{label(slot.slot_id)} · покрытие {covered} из {need} · излишек {Math.max(0, covered - need)}</li>;
      })}</ul>
      <Total total={visible} />
      <Timer expiry={visible.expires_at} remaining={seconds} />
      {!seconds && <div className="notice info">Время подбора истекло. Запросите новый комплект.</div>}
      <button className="button primary" disabled={locked || !seconds || offer.needs_review} onClick={() => void quote()}>Проверить условия</button>
    </section>}
  </div>;
}
