import { Package, Plus } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link, useBlocker, useParams } from 'react-router-dom';
import { ApiError, prepare, read, send } from '../app/api';
import type { DTO } from '../app/api';
import { clean, count, date, integer, label, money, sameOrigin } from '../app/format';
import { useResource } from '../app/resource';
import { useSession } from '../app/session';
import { useScreenNavigation } from '../app/navigation';
import {
  Busy, ConfirmDialog, Copy, Empty, errorsOf, Field, focusError, humanField, Inaccessible,
  Lines, Problem, Success, Tag, Title,
} from '../ui/common';
import { Pager } from './catalog';

type Offer = DTO<'Offer'>;
type Draft = DTO<'DraftContent'>;
type Sku = DTO<'CatalogSku'>;
type Method = Draft['fulfillment_methods'][number];
const methods: Method[] = ['PICKUP', 'COURIER_ZONE_A'];
const steps = ['TASK', 'ITEMS', 'TERMS', 'PREVIEW'] as const;
const captions = ['Задача', 'Товары', 'Получение', 'Проверка'];
const offerPath = (id: string) => '/app/offers/' + id;
const inaccessible = (error: unknown) => error instanceof ApiError && [403, 404].includes(error.status);

export function Offers() {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const page = useResource('listOffers', { query: { limit: 50, cursor: cursors.at(-1) } });
  return <>
    <Title title="Предложения" description="Черновики, опубликованные условия и ссылки для покупателей."
      action={<Link className="button primary" to="/app/offers/new"><Plus size={19} />Новое предложение</Link>} />
    {page.error && <Problem error={page.error} retry={page.reload} />}
    {!page.data ? !page.error && <Busy /> : page.data.items.length === 0
      ? <Empty title="Предложений пока нет" text="Создайте предложение из товаров контрольного каталога."
        action={<Link className="button primary" to="/app/offers/new">Создать предложение</Link>} />
      : <section className="panel rows" aria-label="Список предложений">
        {page.data.items.map(item => <article className="record" key={item.id}>
          <div className="record-icon"><Package size={20} /></div>
          <div className="record-main"><Link to={offerPath(item.id)}>{item.draft.title || 'Предложение без названия'}</Link>
            <small>{label(item.mode)} · {date(item.updated_at)}</small></div>
          <div className="record-status"><Tag value={item.state} />
            {item.needs_review && <small className="attention">Условия требуют проверки</small>}</div>
        </article>)}
      </section>}
    <Pager back={cursors.length > 1 ? () => setCursors(old => old.slice(0, -1)) : undefined}
      next={page.data?.next_cursor ? () => setCursors(old => [...old, page.data!.next_cursor!]) : undefined} />
  </>;
}

export function NewOffer() {
  const { mutate, session, busy, pending } = useSession();
  const navigate = useScreenNavigation();
  const [error, setError] = useState<unknown>(null);
  async function start(mode: Offer['mode']) {
    setError(null);
    try {
      const result = await mutate('createOffer', { body: { mode } });
      navigate(offerPath(result.id) + '/edit', { replace: true });
    } catch (issue) { setError(issue); }
  }
  return <>
    <Title title="Новое предложение" description="Выберите, как покупатель получит состав предложения."
      back={{ to: '/app/offers', text: 'К предложениям' }} />
    <Problem error={error} />
    <div className="cards two">
      <section className="panel stack"><Package size={24} /><h2>Фиксированный состав</h2>
        <p>Один товар или готовый набор из точных SKU. Покупатель выбирает количество одинаковых предложений.</p>
        <button className="button primary" disabled={busy || !!pending} onClick={() => void start('FIXED')}>Создать черновик</button></section>
      {session?.features.bundle && <section className="panel stack"><Package size={24} /><h2>Подбор комплекта</h2>
        <p>Демонстрационный шаблон B001. Сервер подбирает комплект по бюджету, числу гостей и сроку.</p>
        <button className="button secondary" disabled={busy || !!pending} onClick={() => void start('BUNDLE')}>Создать предложение с подбором</button></section>}
    </div>
  </>;
}

function storedDraft(offer: Offer): Draft {
  if (offer.draft_revision !== 1) return offer.draft;
  return { ...offer.draft, fulfillment_methods: ['PICKUP'] };
}

function fieldsOf(draft: Draft) {
  return {
    'Название': draft.title,
    'Описание': draft.description,
    'Источник': label(draft.source),
    'Описание источника': draft.source_text,
    'Товары': draft.lines.map(line => line.sku + ' × ' + line.quantity).join(', '),
    'Шаблон': draft.bundle_template_id ?? '',
    'Получение': draft.fulfillment_methods.map(label).join(', '),
  };
}

function comparePublished(offer: Offer) {
  const { draft, published } = offer;
  if (!published) return false;
  const simplify = (lines: Draft['lines']) => lines.map(({ sku, quantity }) => sku + ':' + quantity).sort();
  return draft.title !== published.title || draft.description !== published.description ||
    draft.source !== published.source || draft.source_text !== published.source_text ||
    draft.bundle_template_id !== (published.bundle_template?.id ?? null) ||
    String([...draft.fulfillment_methods].sort()) !== String([...published.fulfillment_methods].sort()) ||
    String(simplify(draft.lines)) !== String(simplify(published.lines));
}

export function EditOffer() {
  const id = useParams().id!;
  const resource = useResource('getOffer', { path: { offer_id: id } });
  if (inaccessible(resource.error)) return <Inaccessible />;
  if (resource.error && !resource.data) return <Problem error={resource.error} retry={resource.reload} />;
  if (!resource.data) return <Busy text="Загружаем черновик…" />;
  if (resource.data.state === 'ARCHIVED') return <Inaccessible />;
  return <Wizard key={id} initial={resource.data} current={resource.data} />;
}

function Wizard({ initial, current }: { initial: Offer; current: Offer }) {
  const id = initial.id;
  const path = offerPath(id);
  const navigate = useScreenNavigation();
  const { mutate, busy, pending, event, session, revision } = useSession();
  const [baseline, setBaseline] = useState(initial);
  const [draft, setDraft] = useState(() => storedDraft(initial));
  const [quantities, setQuantities] = useState<Record<string, string>>(
    () => Object.fromEntries(initial.draft.lines.map(line => [line.sku, String(line.quantity)])));
  const [task, setTask] = useState(initial.draft.source === 'CONSULTATION' ? 'CONSULTATION'
    : initial.draft.lines.length > 1 ? 'SET' : 'ONE');
  const [step, setStep] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const [success, setSuccess] = useState(false);
  const [touched, mark] = useState<Record<string, boolean>>({});
  const [comparison, compare] = useState<Offer | null>(null);
  const [comparing, setComparing] = useState(false);
  const [catalog, setCatalog] = useState<{ items: Sku[]; error?: unknown; loading: boolean }>({ items: [], loading: true });
  const [catalogRetry, retryCatalog] = useState(0);
  const [search, setSearch] = useState('');
  const form = useRef<HTMLFormElement>(null);
  const allowedLeave = useRef(false);
  const templates = useResource('listBundleTemplates', {}, { enabled: initial.mode === 'BUNDLE' });
  const locked = busy || !!pending || comparing;
  const lines = draftRead();
  const dirty = JSON.stringify(lines) !== JSON.stringify(storedDraft(baseline)) ||
    draft.lines.some(line => integer(quantities[line.sku] ?? '', 1, 99) === null);
  const blocker = useBlocker(() => dirty && !allowedLeave.current);
  const observed = current.revision > baseline.revision;
  const template = templates.data?.items.find(row => row.id === draft.bundle_template_id);
  const detail = useResource('getBundleTemplate', { path: { template_id: template?.id ?? '' } },
    { enabled: !!template });

  useEffect(() => {
    if (!dirty) return;
    const warning = (e: BeforeUnloadEvent) => { if (!allowedLeave.current) e.preventDefault(); };
    window.addEventListener('beforeunload', warning);
    return () => window.removeEventListener('beforeunload', warning);
  }, [dirty]);
  useEffect(() => {
    if (initial.mode !== 'FIXED') return;
    const request = new AbortController();
    async function load() {
      setCatalog(old => ({ ...old, error: undefined, loading: true }));
      let cursor: string | undefined;
      const items: Sku[] = [];
      try {
        do {
          const page: DTO<'CatalogPage'> = await read('listCatalog', { query: { limit: 100, cursor } }, request.signal);
          items.push(...page.items);
          cursor = page.next_cursor ?? undefined;
        } while (cursor);
        if (!request.signal.aborted) setCatalog({ items, loading: false });
      } catch (issue) {
        if (!request.signal.aborted) setCatalog({ items: [], error: issue, loading: false });
      }
    }
    void load();
    return () => request.abort();
  }, [initial.mode, session?.actor_id, session?.generation, revision, catalogRetry]);

  function draftRead(): Draft {
    return { ...draft, title: clean(draft.title), description: clean(draft.description), source_text: clean(draft.source_text),
      lines: draft.lines.map(line => ({ sku: line.sku, quantity: integer(quantities[line.sku] ?? '', 1, 99) ?? line.quantity })) };
  }
  function update<T extends keyof Draft>(key: T, value: Draft[T]) {
    setDraft(old => ({ ...old, [key]: value }));
    setSuccess(false);
    setError(null);
  }
  function restore(next: Offer, preserve = false) {
    setBaseline(next);
    if (!preserve) {
      setDraft(storedDraft(next));
      setQuantities(Object.fromEntries(next.draft.lines.map(line => [line.sku, String(line.quantity)])));
      setTask(next.draft.source === 'CONSULTATION' ? 'CONSULTATION' : next.draft.lines.length > 1 ? 'SET' : 'ONE');
    }
    compare(null);
    setError(null);
  }
  const Issues: Record<string, string> = {};
  for (const key of ['title', 'description', 'source_text'] as const) {
    const max = key === 'title' ? 120 : 1000;
    const text = key === 'title' ? draft[key] : draft[key].replaceAll('\n', '');
    if (count(draft[key]) > max || /[\p{Cc}\p{Cs}]/u.test(text)) Issues[key] = 'Допустимо до ' + max + ' символов без управляющих знаков.';
  }
  draft.lines.forEach(line => {
    if (integer(quantities[line.sku] ?? '', 1, 99) === null) Issues[line.sku] = 'Введите целое количество от 1 до 99.';
  });
  const wire = errorsOf(error);
  const errorAt = (key: string, wireKey = '/' + key) => wire[wireKey] ?? wire['/draft/' + key]
    ?? (touched[key] ? Issues[key] : undefined);
  function textField(key: 'title' | 'description' | 'source_text', caption: string) {
    return <Field label={caption} hint={count(draft[key]) + ' / ' + (key === 'title' ? 120 : 1000)} error={errorAt(key)}>
      {props => key === 'title'
        ? <input {...props} value={draft[key]} onBlur={() => mark(old => ({ ...old, [key]: true }))}
          onChange={e => update(key, e.target.value)} />
        : <textarea {...props} value={draft[key]} rows={3} onBlur={() => mark(old => ({ ...old, [key]: true }))}
          onChange={e => update(key, e.target.value)} />}</Field>;
  }

  function go(next: number) {
    setStep(next);
    event('wizard_step', undefined, steps[next]);
    requestAnimationFrame(() => document.getElementById('wizard-step-heading')?.focus());
  }
  useEffect(() => { event('wizard_step', undefined, 'TASK'); }, [event]);
  function markProblems() {
    const keys = Object.keys(Issues);
    if (!keys.length) return false;
    mark(Object.fromEntries(keys.map(key => [key, true])));
    setStep(keys.some(key => ['title', 'description', 'source_text'].includes(key)) ? 0 : 1);
    focusError(form.current);
    return true;
  }
  function advance() {
    const textFields = ['title', 'description', 'source_text'];
    const invalid = Object.keys(Issues).filter(key => step === 0 ? textFields.includes(key) : step === 1 && !textFields.includes(key));
    if (invalid.length) {
      mark(old => ({ ...old, ...Object.fromEntries(invalid.map(key => [key, true])) }));
      focusError(form.current);
    } else go(step + 1);
  }
  async function loadComparison() {
    setComparing(true);
    try { compare(await read('getOffer', { path: { offer_id: id } })); }
    catch (issue) { setError(issue); }
    finally { setComparing(false); }
  }
  async function save(preview = false) {
    if (locked || comparison || markProblems()) return;
    if (observed) { await loadComparison(); return; }
    setError(null);
    try {
      if (dirty || !preview) {
        const next = await mutate('updateOfferDraft', { path: { offer_id: id },
          body: { expected_revision: baseline.revision, draft: lines } });
        restore(next);
        setSuccess(true);
      }
      if (preview) {
        allowedLeave.current = true;
        navigate(path + '/preview');
      }
    } catch (issue) {
      setError(issue);
      if (issue instanceof ApiError && issue.code === 'REVISION_CONFLICT') await loadComparison();
      else {
        const paths = Object.keys(errorsOf(issue));
        if (paths.length) setStep(paths.some(key => /^\/(?:draft\/)?(title|description|source)/.test(key)) ? 0 :
          paths.some(key => key.includes('fulfillment')) ? 2 : 1);
        focusError(form.current);
      }
    }
  }
  function add(item: Sku) {
    if (!draft.lines.some(x => x.sku === item.sku)) {
      if (draft.lines.length >= 20) return;
      update('lines', [...draft.lines, { sku: item.sku, quantity: 1 }]);
      setQuantities(old => ({ ...old, [item.sku]: '1' }));
    }
    requestAnimationFrame(() => document.getElementById('qty-' + item.sku)?.focus());
  }
  const selected = lines.lines.map(line => ({ ...line, item: catalog.items.find(item => item.sku === line.sku) }));
  const sample = selected.flatMap(({ item, sku, quantity }) => item ? [{
    sku, quantity, title: item.title, unit_price_minor: item.price_minor, line_total_minor: item.price_minor * quantity,
  }] : []);
  const shown = catalog.items.filter(item => (item.sku + ' ' + item.title).toLowerCase().includes(search.toLowerCase()));
  return <>
    <Title title="Мастер предложения" description="Состав и условия черновика хранятся отдельно от публикации."
      back={{ to: path, text: 'К карточке предложения' }} />
    <div className="wizard-layout">
      <ol className="panel steps" aria-label="Шаги мастера">{captions.map((caption, index) => <li key={caption}>
        <button type="button" disabled={locked} className={index === step ? 'selected' : ''}
          aria-current={index === step ? 'step' : undefined} onClick={() => go(index)}>
          <span>{index + 1}</span>{caption}</button></li>)}</ol>
      <div className="stack">
        {initial.published_version_no && <div className="notice info"><b>Опубликована версия {initial.published_version_no}.</b>
          {(dirty || comparePublished(baseline)) && ' Изменения черновика ещё не опубликованы.'}</div>}
        {success && <Success>Черновик сохранён.</Success>}
        <Problem error={error} detail={!Object.keys(wire).length} />
        {observed && !comparison && <div className="notice issue"><div><b>Данные изменились в другом окне.</b>
          <button className="button secondary" disabled={locked} onClick={() => void loadComparison()}>Загрузить и сравнить</button></div></div>}
        {comparison && <section className="panel stack"><h2>Сравнение черновиков</h2>
          {Object.entries(fieldsOf(lines)).map(([key, value]) => <dl className="comparison" key={key}><dt>{key}</dt>
            <dd><small>Мои значения</small><span>{value || 'Пусто'}</span></dd>
            <dd><small>Актуальные значения</small><span>{fieldsOf(comparison.draft)[key as keyof ReturnType<typeof fieldsOf>] || 'Пусто'}</span></dd></dl>)}
          <div className="actions"><button className="button secondary" onClick={() => restore(comparison)}>Загрузить актуальное</button>
            {comparison.state !== 'ARCHIVED' && <button className="button secondary" onClick={() => restore(comparison, true)}>Вернуться к своим правкам</button>}</div>
        </section>}
        <form ref={form} className="panel stack" noValidate onSubmit={e => {
          e.preventDefault();
          if (locked) return;
          if (step < 3) advance(); else void save();
        }}>
          <h2 tabIndex={-1} id="wizard-step-heading">{captions[step]}</h2>
          <fieldset disabled={locked || comparison?.state === 'ARCHIVED'} className="stack">
            {step === 0 && <>
              {initial.mode === 'FIXED' && <fieldset className="choice-field"><legend>Что вы предлагаете?</legend>
                <div className="choices">{([['ONE', 'Один товар'], ['SET', 'Готовый набор'], ['CONSULTATION', 'После консультации']] as const)
                  .map(([value, caption]) => <label className="choice" key={value}><input type="radio" name="task" value={value} checked={task === value}
                    onChange={() => {
                      setTask(value);
                      update('source', value === 'CONSULTATION' ? 'CONSULTATION' : draft.source === 'POST' ? 'POST' : 'MANUAL');
                    }} />{caption}</label>)}</div>
              </fieldset>}
              {textField('title', 'Название для публикации (обязательно)')}
              {textField('description', 'Описание')}
              {initial.mode === 'FIXED' && task !== 'CONSULTATION' && <Field label="Источник предложения" error={errorAt('source')}>
                {props => <select {...props} value={draft.source} onChange={e => update('source', e.target.value as Draft['source'])}>
                  <option value="MANUAL">Вручную</option><option value="POST">Из публикации</option>
                </select>}</Field>}
              {textField('source_text', 'Учебное описание источника')}
            </>}
            {step === 1 && (initial.mode === 'FIXED' ? <>
              <Field label="Найти товар по названию или SKU">{props => <input {...props} type="search" value={search} onChange={e => setSearch(e.target.value)} />}</Field>
              {catalog.loading && <Busy text="Загружаем каталог…" />}
              <Problem error={catalog.error} retry={() => retryCatalog(n => n + 1)} />
              <div className="pick-list" aria-label="Товары каталога">
                {shown.map(item => {
                  const chosen = draft.lines.some(x => x.sku === item.sku);
                  return <div className="pick-row" key={item.sku}><div><b>{item.title}</b>
                    <small>{item.sku} · {money(item.price_minor)} · {item.active ? 'Доступно ' + item.stock_available : 'Неактивен'}</small></div>
                    <button type="button" className="button secondary" aria-label={(chosen ? 'Перейти к количеству ' : 'Добавить ') + item.sku}
                      disabled={!chosen && (draft.lines.length >= 20 || !item.active || item.stock_available === 0)}
                      onClick={() => add(item)}>{chosen ? 'Количество' : 'Добавить'}</button></div>;
                })}
                {!shown.length && !catalog.loading && !catalog.error && <p>Товары не найдены.</p>}
              </div>
              <h3>Выбранные товары ({draft.lines.length}/20)</h3>
              {!selected.length && <p>В составе пока нет товаров.</p>}
              {selected.map(({ sku, item }, index) => <div className="pick-row chosen" key={sku}>
                <div><b>{item?.title ?? sku}</b><small>{sku}{item && ' · Доступно ' + item.stock_available}</small>
                  {item && (!item.active || item.stock_available < (lines.lines[index]?.quantity ?? 1)) && <small className="attention">Товар недоступен в этом количестве</small>}</div>
                <Field label={'Количество ' + sku} id={'qty-' + sku} error={errorAt(sku, '/draft/lines/' + index + '/quantity')}>
                  {props => <input {...props} inputMode="numeric" value={quantities[sku] ?? ''} onBlur={() => mark(old => ({ ...old, [sku]: true }))}
                    onChange={e => { setQuantities(old => ({ ...old, [sku]: e.target.value })); setSuccess(false); }} />}</Field>
                <button className="button secondary" type="button" aria-label={'Убрать ' + sku}
                  onClick={() => update('lines', draft.lines.filter(line => line.sku !== sku))}>Убрать</button>
              </div>)}
            </> : <>
              <Problem error={templates.error ?? detail.error} retry={templates.error ? templates.reload : detail.reload} />
              {templates.loading && <Busy text="Загружаем шаблоны…" />}
              <Field label="Шаблон комплекта (обязательно)" error={errorAt('bundle_template_id')}>{props =>
                <select {...props} value={draft.bundle_template_id ?? ''} onChange={e => update('bundle_template_id', e.target.value || null)}>
                  <option value="">Выберите шаблон</option>
                  {templates.data?.items.map(row => <option key={row.id} value={row.id}>{row.code} · {row.title}</option>)}</select>}</Field>
              {detail.data && <Template template={detail.data} />}
            </>)}
            {step === 2 && <fieldset className="choice-field">
              <legend>Способы получения (для публикации нужен хотя бы один)</legend>
              {methods.map(value => <label className="choice" key={value}><input type="checkbox"
                checked={draft.fulfillment_methods.includes(value)}
                onChange={e => update('fulfillment_methods', e.target.checked ? [...draft.fulfillment_methods, value]
                  : draft.fulfillment_methods.filter(method => method !== value))} />
                {label(value)} · {money(value === 'PICKUP' ? 0 : 30000)}</label>)}
              <p>Курьерская зона A условная. Стоимость получения начисляется один раз за заказ.</p>
              {errorAt('fulfillment_methods') && <p className="field-error">{errorAt('fulfillment_methods')}</p>}
            </fieldset>}
            {step === 3 && <>
              <h3>{draft.title || 'Предложение без названия'}</h3><p>{draft.description}</p>
              <div className="details"><span>Источник <b>{label(draft.source)}</b></span>
                <span>Получение <b>{draft.fulfillment_methods.map(label).join(', ') || 'Не выбрано'}</b></span></div>
              {initial.mode === 'FIXED' ? <>
                <Lines lines={sample} />
                {sample.length > 0 && sample.length === selected.length &&
                  <div className="amount-label">По текущим ценам каталога <strong>{money(sample.reduce((sum, line) => sum + line.line_total_minor, 0))}</strong></div>}
              </> : template && <Template template={template} />}
              {(!clean(draft.title).trim() || !draft.fulfillment_methods.length ||
                (initial.mode === 'FIXED' ? !lines.lines.length : !template)) &&
                <div className="notice info">Черновик можно сохранить. Для публикации заполните название, состав и получение.</div>}
              {selected.some(({ item, quantity }) => !item || !item.active || item.stock_available < quantity) &&
                <div className="notice issue">Проверьте наличие выбранных товаров перед публикацией.</div>}
            </>}
            {Object.entries(wire).filter(([key]) => !/^\/(draft\/)?(title|description|source_text|source|fulfillment_methods|bundle_template_id)$/.test(key) &&
              !/^\/draft\/lines\/\d+\/quantity$/.test(key)).map(([key, value]) =>
              <p className="field-error" key={key}>{humanField(key)} {value}</p>)}
            <div className="actions wizard-actions">
              {step > 0 && <button type="button" className="button secondary" onClick={() => go(step - 1)}>Назад</button>}
              <button className="button secondary" type="button" disabled={!!comparison} onClick={() => void save()}>Сохранить черновик</button>
              {step < 3 ? <button type="submit" className="button primary">Далее</button> :
                <button type="button" className="button primary" disabled={!!comparison} onClick={() => void save(true)}>К предпросмотру</button>}
            </div>
          </fieldset>
        </form>
      </div>
    </div>
    {blocker.state === 'blocked' && <ConfirmDialog title="Выйти из мастера?" confirm="Выйти без сохранения"
      approve={() => blocker.proceed()} dismiss={() => blocker.reset()}>
      <p>Введённые изменения ещё не сохранены.</p>
    </ConfirmDialog>}
  </>;
}

export function Template({ template }: { template: DTO<'BundleTemplate'> }) {
  return <section className="template stack">
    <div><h3>{template.title}</h3><p>{template.code} · версия {template.version_no} · контрольный шаблон только для чтения</p></div>
    <ul className="compact-list">{template.slots.map(slot => <li key={slot.slot_id}>{label(slot.slot_id)}</li>)}</ul>
  </section>;
}

export function OfferPreview() {
  const id = useParams().id!;
  const path = offerPath(id);
  const offer = useResource('getOffer', { path: { offer_id: id } });
  const { session, mutate, busy, pending, event } = useSession();
  const navigate = useScreenNavigation();
  const [result, setResult] = useState<DTO<'OfferPreview'> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [commandError, setCommandError] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  const expected = offer.data?.state === 'ARCHIVED' ? undefined : offer.data?.revision;
  const csrf = session?.csrf_token;
  useEffect(() => {
    if (!expected || !csrf) return;
    const request = new AbortController();
    setError(null);
    setResult(null);
    void send(prepare('previewOffer', { path: { offer_id: id }, body: { expected_revision: expected } }), csrf, request.signal)
      .then(answer => { if (!request.signal.aborted) setResult(answer.data); })
      .catch(issue => { if (!request.signal.aborted) setError(issue); });
    return () => request.abort();
  }, [id, expected, csrf, retry]);
  async function publish() {
    if (!result?.valid || result.revision !== expected || commandError instanceof ApiError && commandError.code === 'REVISION_CONFLICT') return;
    setCommandError(null);
    try {
      const item = await mutate('publishOffer', { path: { offer_id: id }, body: { expected_revision: result.revision } });
      event('wizard_step', item.published_version_no ? { offerId: id, version: item.published_version_no } : undefined, 'PUBLISHED');
      navigate(path, { state: { published: true } });
    } catch (issue) {
      setCommandError(issue);
      if (issue instanceof ApiError && issue.status === 409) {
        await offer.reload();
        setRetry(n => n + 1);
      }
    }
  }
  if (inaccessible(error ?? offer.error) || offer.data?.state === 'ARCHIVED') return <Inaccessible />;
  return <>
    <Title title="Предпросмотр предложения" back={{ to: path + '/edit', text: 'К черновику' }} />
    <div className="notice info"><b>Предпросмотр · заказ недоступен</b></div>
    <Problem error={offer.error} retry={offer.reload} />
    <Problem error={error} retry={() => { void offer.reload(); setRetry(n => n + 1); }} />
    <Problem error={commandError} />
    {!offer.data || !result || result.revision !== expected ? !error && !offer.error && <Busy text="Проверяем черновик…" />
      : <div className="split">
        <section className="panel stack"><h2>{offer.data.draft.title || 'Предложение без названия'}</h2>
          {offer.data.draft.description && <p className="multiline">{offer.data.draft.description}</p>}
          <p>Получение · {offer.data.draft.fulfillment_methods.map(label).join(', ') || 'Не выбрано'}</p>
          <Lines lines={result.priced_lines} />
          {result.bundle_template && <Template template={result.bundle_template} />}
          {result.issues.length > 0 && <div className="notice issue" role="alert"><div><b>Проверьте условия перед публикацией.</b>
            <ul>{result.issues.map((issue, index) => <li key={index}>{humanField(issue.path)} {issue.message}</li>)}</ul></div></div>}
        </section>
        <aside className="panel stack summary"><h2>Публикация в демо</h2>
          {result.subtotal_minor !== null && <div className="amount-label">Товары <strong>{money(result.subtotal_minor)}</strong></div>}
          <p>Вы получите ссылку для покупателя в этой демонстрации. Пост во ВКонтакте не создаётся.</p>
          <button className="button primary" disabled={!result.valid || busy || !!pending ||
            commandError instanceof ApiError && commandError.code === 'REVISION_CONFLICT'}
            onClick={() => void publish()}>Опубликовать в демонстрации</button>
          {!result.valid || commandError ? <Link className="button secondary" to={path + '/edit'}>Исправить черновик</Link> : null}
          {offer.data.published && <Link className="button secondary" to={path}>Открыть действующее предложение</Link>}
        </aside>
      </div>}
  </>;
}

export function OfferDetail() {
  const id = useParams().id!;
  const path = offerPath(id);
  const page = useResource('getOffer', { path: { offer_id: id } });
  const { mutate, busy, pending } = useSession();
  const [action, setAction] = useState<'pauseOffer' | 'archiveOffer' | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [changed, setChanged] = useState(false);
  if (inaccessible(page.error)) return <Inaccessible />;
  if (!page.data) return page.error ? <Problem error={page.error} retry={page.reload} /> : <Busy />;
  const offer = page.data;
  const share = offer.share_url && sameOrigin(offer.share_url, /^\/buy\/[0-9a-f-]{36}$/);
  const canShare = share && !share.hash && !share.search && share.pathname === '/buy/' + id && offer.state === 'ACTIVE';
  async function change() {
    if (!action) return;
    setError(null);
    try {
      await mutate(action, { path: { offer_id: id }, body: { expected_revision: offer.revision } });
      setChanged(true);
      setAction(null);
      await page.reload();
    } catch (issue) {
      setError(issue);
      setAction(null);
      if (issue instanceof ApiError) await page.reload();
    }
  }
  return <>
    <Title title={offer.draft.title || 'Предложение без названия'}
      back={{ to: '/app/offers', text: 'К предложениям' }}
      description={label(offer.mode)}
      action={offer.state !== 'ARCHIVED' && <Link className="button primary" to={path + '/edit'}>Редактировать черновик</Link>} />
    {changed && <Success>Состояние предложения обновлено.</Success>}
    <Problem error={error ?? page.error} retry={page.reload} />
    <div className="split">
      <section className="panel stack">
        <div className="section-heading"><h2>{offer.published ? 'Опубликованная версия ' + offer.published.version_no : 'Черновик'}</h2>
          <Tag value={offer.state} /></div>
        {offer.state === 'PAUSED' && <div className="notice info">Продажи приостановлены.</div>}
        {offer.needs_review && <div className="notice issue">Продавцу нужно обновить условия предложения.</div>}
        {comparePublished(offer) && <div className="notice info">Изменения черновика ещё не опубликованы.</div>}
        {offer.published && <>
          <h3>{offer.published.title}</h3>
          {offer.published.description && <p className="multiline">{offer.published.description}</p>}
          <Lines lines={offer.published.lines} />
          {offer.published.bundle_template && <Template template={offer.published.bundle_template} />}
          <p>Получение · {offer.published.fulfillment_methods.map(label).join(', ')}</p>
        </>}
        {canShare && <div className="stack"><h3>Ссылка на предложение</h3>
          <p>Передайте покупателю, который уже открыл отдельную ссылку своей роли.</p><Copy key={share.href} value={share.href} /></div>}
      </section>
      <aside className="panel stack summary"><h2>Управление</h2>
        <div className="details"><span>Редакция черновика <b>{offer.draft_revision}</b></span>
          <span>Обновлено <b>{date(offer.updated_at)}</b></span></div>
        {offer.state !== 'ARCHIVED' && <>
          <Link className="button secondary" to={path + '/preview'}>Проверить перед публикацией</Link>
          {offer.state === 'ACTIVE' && <button className="button secondary" disabled={busy || !!pending}
            onClick={() => setAction('pauseOffer')}>Приостановить продажи</button>}
          <button className="button danger-soft" disabled={busy || !!pending}
            onClick={() => setAction('archiveOffer')}>Архивировать</button>
        </>}
      </aside>
    </div>
    {action && <ConfirmDialog title={action === 'pauseOffer' ? 'Приостановить продажи?' : 'Архивировать предложение?'}
      confirm={action === 'pauseOffer' ? 'Приостановить' : 'Архивировать'} disabled={busy || !!pending}
      approve={() => void change()} dismiss={() => setAction(null)}>
      <p>Новые оформления будут недоступны. Активные резервы этого предложения будут отменены. Уже созданные заказы сохранятся.</p>
    </ConfirmDialog>}
  </>;
}
