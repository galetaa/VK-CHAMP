import { Layers3, Pencil, Plus, ScanBarcode } from 'lucide-react';
import { useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, read } from '../app/api';
import type { DTO } from '../app/api';
import { clean, count, decimal, integer, kop, money } from '../app/format';
import { useResource } from '../app/resource';
import { useSession } from '../app/session';
import { Busy, Empty, errorsOf, Field, focusError, Problem, Success, Title } from '../ui/common';

type Sku = DTO<'CatalogSku'>;
type Form = { sku: string; title: string; price: string; stock: string; active: boolean; days: string; units: string };
const blank: Form = { sku: '', title: '', price: '', stock: '0', active: true, days: '1', units: '1' };
const fromSku = (item: Sku): Form => ({
  sku: item.sku, title: item.title, price: decimal(item.price_minor), stock: String(item.stock_on_hand),
  active: item.active, days: String(item.ready_in_days), units: String(item.units_per_pack),
});

async function loadSku(sku: string) {
  let cursor: string | null = null;
  do {
    const page: DTO<'CatalogPage'> = await read('listCatalog', { query: { limit: 100, cursor: cursor ?? undefined } });
    const item = page.items.find(row => row.sku === sku);
    if (item) return item;
    cursor = page.next_cursor;
  } while (cursor);
  throw new Error('Товар отсутствует в текущем каталоге.');
}

function validate(values: Form) {
  const result: Record<string, string> = {};
  if (!/^[A-Z0-9_-]{1,40}$/.test(values.sku)) result.sku = 'Введите 1–40 символов A–Z, цифры, дефис или подчёркивание.';
  if (!clean(values.title).trim() || count(values.title) > 120 || /[\p{Cc}\p{Cs}]/u.test(values.title)) {
    result.title = 'Введите название от 1 до 120 символов одной строкой.';
  }
  if (kop(values.price) === null) result.price = 'Введите цену от 0,01 до 1 000 000,00 ₽.';
  if (integer(values.stock, 0, 99999) === null) result.stock = 'Введите целый остаток от 0 до 99 999.';
  if (integer(values.days, 1, 30) === null) result.days = 'Введите целое число от 1 до 30.';
  if (integer(values.units, 1, 100) === null) result.units = 'Введите целое число от 1 до 100.';
  return result;
}

function SkuEditor({ item, dismiss, saved }: { item: Sku | null; dismiss: () => void; saved: () => void }) {
  const { mutate, busy, pending } = useSession();
  const [baseline, setBaseline] = useState(item);
  const [values, setValues] = useState<Form>(item ? fromSku(item) : blank);
  const [error, setError] = useState<unknown>(null);
  const [touched, mark] = useState<Record<string, boolean>>({});
  const [comparison, compare] = useState<Sku | null>(null);
  const [comparing, setComparing] = useState(false);
  const ref = useRef<HTMLFormElement>(null);
  const locked = busy || !!pending;
  const issues = validate(values);
  const wire = errorsOf(error);
  if (error instanceof ApiError && error.code === 'STOCK_BELOW_RESERVED') {
    wire['/stock_on_hand'] = 'Часть товара уже зарезервирована.';
  }
  if (error instanceof ApiError && error.code === 'STOCK_CAPACITY_CONFLICT') {
    wire['/stock_on_hand'] = 'Значение слишком велико с учётом возможного возврата из незавершённых заказов.';
  }
  const update = (key: keyof Form, value: string | boolean) => {
    setValues(previous => ({ ...previous, [key]: value }));
    setError(null);
  };
  const input = (key: Exclude<keyof Form, 'active'>, title: string, options: {
    path?: string; disabled?: boolean; mode?: 'numeric' | 'decimal'; hint?: string;
  } = {}) => <Field label={title} error={wire[options.path ?? '/' + key] ?? (touched[key] ? issues[key] : undefined)}
    hint={options.hint}>{props => <input {...props} value={values[key]} inputMode={options.mode}
      disabled={locked || options.disabled} autoComplete="off" onBlur={() => mark(old => ({ ...old, [key]: true }))}
      onChange={event => update(key, event.target.value)} />}</Field>;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (locked || comparison) return;
    mark(Object.fromEntries(Object.keys(blank).map(key => [key, true])));
    if (Object.keys(issues).length) { focusError(ref.current); return; }
    const data = {
      title: clean(values.title), price_minor: kop(values.price)!, stock_on_hand: integer(values.stock, 0, 99999)!,
      active: values.active, ready_in_days: integer(values.days, 1, 30)!,
    };
    try {
      if (baseline) {
        await mutate('updateSku', { path: { sku: baseline.sku }, body: { ...data, expected_revision: baseline.revision } });
      } else {
        await mutate('createSku', { body: { ...data, sku: values.sku, units_per_pack: integer(values.units, 1, 100)! } });
      }
      saved();
    } catch (issue) {
      setError(issue);
      focusError(ref.current);
      if (baseline && issue instanceof ApiError && issue.code === 'REVISION_CONFLICT') {
        setComparing(true);
        try { compare(await loadSku(baseline.sku)); } catch (lookup) { setError(lookup); }
        finally { setComparing(false); }
      }
    }
  }
  return <section className="panel editor stack" aria-label={baseline ? 'Редактирование товара' : 'Новый товар'}>
    <div className="section-heading"><ScanBarcode size={22} /><h2>{baseline ? 'Редактирование ' + baseline.sku : 'Новый товар'}</h2></div>
    <form noValidate onSubmit={submit} ref={ref} className="stack">
      <div className="form-grid">
        {input('sku', 'Код SKU · обязательно', { disabled: !!baseline })}
        {input('title', 'Название · обязательно', { hint: count(values.title) + ' / 120' })}
        {input('price', 'Цена, ₽ · обязательно', { mode: 'decimal', path: '/price_minor' })}
        {input('stock', 'Физический остаток · обязательно', { mode: 'numeric', path: '/stock_on_hand',
          hint: baseline ? 'Сейчас удержано: ' + baseline.stock_reserved : undefined })}
        {input('days', 'Готовность, дней · обязательно', { mode: 'numeric', path: '/ready_in_days' })}
        {input('units', 'Единиц в упаковке · обязательно', { mode: 'numeric', disabled: !!baseline, path: '/units_per_pack' })}
      </div>
      <label className="check"><input type="checkbox" checked={values.active} disabled={locked}
        onChange={event => update('active', event.target.checked)} />Доступен для продажи</label>
      {baseline && (baseline.price_minor !== kop(values.price) || baseline.active !== values.active) &&
        <p className="soft-note">Для новых покупок может потребоваться перепубликация. Активные резервы сохраняют согласованную цену.</p>}
      <Problem error={error} />
      {comparing && <Busy text="Получаем актуальные значения…" />}
      {comparison && <div className="compare" role="region" aria-label="Сравнение товара">
        <h3>Сравните значения товара</h3>
        <dl>{(['title', 'price', 'stock', 'days', 'active'] as const).map(key => <div key={key}>
          <dt>{{ title: 'Название', price: 'Цена, ₽', stock: 'Остаток', days: 'Готовность, дней', active: 'Доступность' }[key]}</dt>
          <dd>Ваш ввод: {display(values[key])}</dd><dd>На сервере: {display(fromSku(comparison)[key])}</dd>
        </div>)}</dl>
        <div className="actions">
          <button type="button" className="button secondary" onClick={() => {
            setValues(fromSku(comparison)); setBaseline(comparison); compare(null); setError(null);
          }}>Загрузить актуальные</button>
          <button type="button" className="button secondary" onClick={() => {
            setBaseline(comparison); compare(null); setError(null);
          }}>Вернуться к редактированию</button>
        </div>
      </div>}
      <div className="actions"><button className="button primary" disabled={locked || comparing || !!comparison}>Сохранить товар</button>
        <button type="button" className="button secondary" onClick={dismiss} disabled={locked}>Закрыть</button></div>
    </form>
  </section>;
}
const display = (value: string | boolean) => typeof value === 'boolean' ? value ? 'Доступен' : 'Выключен' : value;

export function Catalog() {
  const { session, pending } = useSession();
  const [cursor, setCursor] = useState<string[]>([]);
  const [editor, setEditor] = useState<Sku | null | undefined>();
  const [done, doneMessage] = useState(false);
  const page = useResource('listCatalog', { query: { cursor: cursor.at(-1), limit: 50 } });
  const owner = session?.role === 'OWNER';
  return <>
    <Title title="Каталог" description={owner ? 'Цена, наличие и готовность товаров к выдаче.' : 'Изменяет владелец.'}
      action={owner && <button className="button primary" disabled={!!pending}
        onClick={() => { setEditor(null); doneMessage(false); }}><Plus size={19} />Новый SKU</button>} />
    {done && <Success>Товар сохранён.</Success>}
    {editor !== undefined && <SkuEditor key={editor?.sku ?? 'new'} item={editor} dismiss={() => setEditor(undefined)}
      saved={() => { setEditor(undefined); doneMessage(true); }} />}
    <Problem error={page.error} retry={() => { void page.reload(); }} />
    {!page.data && page.loading ? <Busy text="Загружаем каталог…" /> : page.data && (page.data.items.length ? <>
      <div className="panel list-panel"><div className="section-heading"><Layers3 size={21} /><h2>Товары</h2></div>
        <ul className="catalog-list">{page.data.items.map(item => <li key={item.sku} className="catalog-row">
          <div className="sku-name"><span className="sku-icon"><ScanBarcode size={20} /></span>
            <div><b>{item.title}</b><small>SKU {item.sku} · {item.units_per_pack} в упаковке</small></div></div>
          <div className="sku-price"><strong>{money(item.price_minor)}</strong>
            <span className={'pill ' + (item.active ? 'good' : 'quiet')}>{item.active ? 'В продаже' : 'Выключен'}</span></div>
          <dl className="sku-stock">
            <div><dt>Остаток</dt><dd>{item.stock_on_hand}</dd></div>
            <div><dt>В резерве</dt><dd>{item.stock_reserved}</dd></div>
            <div><dt>Доступно</dt><dd>{item.stock_available}</dd></div>
            <div><dt>Дней</dt><dd>{item.ready_in_days}</dd></div>
          </dl>
          {owner && <button className="button secondary small icon-only" aria-label={'Редактировать ' + item.sku}
            disabled={!!pending} onClick={() => { setEditor(item); doneMessage(false); }}><Pencil size={17} /></button>}
        </li>)}</ul>
      </div>
      <Pager back={cursor.length > 0 ? () => setCursor(previous => previous.slice(0, -1)) : undefined}
        next={page.data.next_cursor ? () => setCursor(previous => [...previous, page.data!.next_cursor!]) : undefined} />
    </> : <Empty title="В каталоге пока нет товаров" text={owner ? 'Добавьте первый точный вариант товара.' : undefined} />)}
  </>;
}

export function Pager({ back, next }: { back?: () => void; next?: () => void }) {
  if (!back && !next) return null;
  return <nav aria-label="Страницы списка" className="actions">
    {back && <button className="button secondary" onClick={back}>Предыдущая страница</button>}
    {next && <button className="button secondary" onClick={next}>Следующая страница</button>}
  </nav>;
}
