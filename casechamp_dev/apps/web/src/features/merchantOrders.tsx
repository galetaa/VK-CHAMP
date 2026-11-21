import { ClipboardList, ShoppingBag } from 'lucide-react';
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError } from '../app/api';
import type { DTO } from '../app/api';
import { date, label, money } from '../app/format';
import { liveOrder, useResource } from '../app/resource';
import { useSession } from '../app/session';
import { Busy, ConfirmDialog, Empty, Field, Inaccessible, Lines, Problem, Success, Tag, Title, Total } from '../ui/common';
import { PurchaseFacts } from './buyerCheckout';
import { Pager } from './catalog';

type Order = DTO<'Order'>;
type Transition = DTO<'OrderTransitionRequest'>['to_status'];
type Reason = NonNullable<DTO<'OrderTransitionRequest'>['cancel_reason']>;
const reasons: Reason[] = ['CUSTOMER_REQUEST', 'CANNOT_FULFILL', 'DEMO_CORRECTION'];

export function Orders({ buyer = false }: { buyer?: boolean }) {
  const [pages, setPages] = useState<string[]>([]);
  const page = useResource('listOrders', { query: { limit: 50, cursor: pages.at(-1) } });
  return <>
    <Title title={buyer ? 'Мои тестовые заказы' : 'Заказы'} description={buyer
      ? 'Откройте ссылку на предложение, которую передал продавец.'
      : 'Очередь тестовых заказов из этой демонстрации.'} />
    <Problem error={page.error} retry={page.reload} />
    {!page.data ? !page.error && <Busy text="Загружаем заказы…" /> : page.data.items.length === 0
      ? <Empty title="Заказов пока нет" text={buyer ? 'Здесь появятся ваши подтверждённые заказы.' :
        'Здесь появятся заказы после подтверждения покупателем.'}
        action={buyer ? undefined : <Link className="button secondary" to="/app/offers">К предложениям</Link>} />
      : <section className="panel rows" aria-label="Список заказов">
        {page.data.items.map(item => <article className="record" key={item.id} data-order-id={item.id}>
          <div className="record-icon"><ShoppingBag size={20} /></div>
          <div className="record-main"><Link to={(buyer ? '/buy/orders/' : '/app/orders/') + item.id}>
            Заказ {item.id.slice(0, 8)}</Link><small>{date(item.created_at)}</small></div>
          <strong>{money(item.total_minor)}</strong><div className="record-status"><Tag value={item.status} /></div>
        </article>)}
      </section>}
    <Pager back={pages.length ? () => setPages(old => old.slice(0, -1)) : undefined}
      next={page.data?.next_cursor ? () => setPages(old => [...old, page.data!.next_cursor!]) : undefined} />
  </>;
}

export function OrderPage({ buyer = false }: { buyer?: boolean }) {
  const id = useParams().id!;
  const resource = useResource('getOrder', { path: { order_id: id } }, { interval: 5000, when: liveOrder });
  if (resource.error instanceof ApiError && [403, 404].includes(resource.error.status)) return <Inaccessible />;
  if (!resource.data) return resource.error
    ? <Problem error={resource.error} retry={resource.reload} /> : <Busy text="Загружаем заказ…" />;
  return <OrderCard key={id} item={resource.data} buyer={buyer} reload={resource.reload} />;
}

function OrderCard({ item, buyer, reload }: { item: Order; buyer: boolean; reload: () => Promise<Order | undefined> }) {
  const { mutate, pending, busy } = useSession();
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const [cancel, setCancel] = useState(false);
  const [reason, setReason] = useState<Reason | ''>('');
  const active = liveOrder(item);
  const locked = !!pending || busy;
  const facts: [string, string | null][] = [
    ['Создан', item.created_at], ['Принят', item.accepted_at],
    ['Исполнен', item.fulfilled_at], ['Отменён', item.cancelled_at],
  ];
  async function change(to_status: Transition) {
    if (!active || locked || to_status === 'CANCELLED' && !reason) return;
    setDone(false);
    setError(null);
    try {
      await mutate('transitionOrder', { path: { order_id: item.id }, body: {
        expected_revision: item.revision, to_status, cancel_reason: to_status === 'CANCELLED' ? reason || null : null,
      } });
      setCancel(false);
      setDone(true);
      await reload();
    } catch (issue) {
      setCancel(false);
      setError(issue);
      if (issue instanceof ApiError && issue.status === 409) await reload();
    }
  }
  return <>
    <Title title="Тестовый заказ приложения" description={'Заказ ' + item.id.slice(0, 8)}
      back={{ to: buyer ? '/' : '/app/orders', text: buyer ? 'К моим заказам' : 'К очереди заказов' }} />
    <Problem error={error} retry={reload}
      explanation={error instanceof ApiError && error.code === 'REVISION_CONFLICT' ? 'Заказ уже обработан. Проверьте актуальный статус.' : undefined} />
    {done && <Success>Статус заказа обновлён.</Success>}
    <div className="split">
      <div className="stack">
        <section className="panel stack">
          <div className="section-heading"><h2>Состав заказа</h2><Tag value={item.status} /></div>
          <Lines lines={item.lines} /><PurchaseFacts data={item} />
        </section>
        <section className="panel stack">
          <div className="section-heading"><ClipboardList size={20} /><h2>Этапы заказа</h2></div>
          <ol className="timeline">{facts.map(([stage, instant]) => instant && <li key={stage}>
            <b>{stage}</b><time dateTime={instant}>{date(instant)}</time></li>)}</ol>
          {item.cancel_reason && <p>Причина отмены · {label(item.cancel_reason)}</p>}
          <small className="muted">ID заказа {item.id}</small>
        </section>
      </div>
      <aside className="panel summary stack"><h2>Стоимость заказа</h2><Total total={item} />
        <div className="notice info"><div><b>DEMO · Тестовый заказ</b><p>Платёж не выполнялся.</p></div></div>
        {!buyer && active && <>
          <button disabled={locked} className="button primary" onClick={() => void change(item.status === 'CREATED' ? 'ACCEPTED' : 'FULFILLED')}>
            {item.status === 'CREATED' ? 'Принять' : 'Отметить исполненным'}</button>
          <button disabled={locked} className="button danger-soft" onClick={() => { setReason(''); setCancel(true); }}>Отменить</button>
        </>}
      </aside>
    </div>
    {cancel && <ConfirmDialog title="Отменить тестовый заказ и вернуть товары в доступный остаток?"
      confirm="Отменить заказ" disabled={locked} invalid={!reason || !active} dismiss={() => setCancel(false)}
      approve={() => void change('CANCELLED')}>
      <Field label="Причина отмены (обязательно)">{props => <select {...props}
        value={reason} disabled={locked} onChange={e => setReason(e.target.value as Reason | '')}>
        <option value="">Выберите причину</option>
        {reasons.map(value => <option key={value} value={value}>{label(value)}</option>)}
      </select>}</Field>
    </ConfirmDialog>}
  </>;
}
