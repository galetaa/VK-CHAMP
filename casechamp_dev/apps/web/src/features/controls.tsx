import { Activity, Box, ChartNoAxesColumnIncreasing, Clock4, FileText, Link2, RotateCcw, ShoppingBag, UserRoundPlus } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import type { DTO } from '../app/api';
import { date, money, sameOrigin } from '../app/format';
import { useResource } from '../app/resource';
import { useSession } from '../app/session';
import { useScreenNavigation } from '../app/navigation';
import { Busy, ConfirmDialog, Copy, Problem, Title } from '../ui/common';

export function Dashboard() {
  const { session } = useSession();
  const query = useResource('getDashboard', {}, { interval: 10_000 });
  const data = query.data;
  const empty = data && data.published_offers === 0;
  return <>
    <Title title="Рабочий стол" description="Предложения, расчёты и тестовые заказы текущей демонстрации."
      action={<Link className="button primary" to="/app/offers/new"><FileText size={18} />Создать предложение</Link>} />
    <Problem error={query.error} retry={() => { void query.reload(); }} />
    {!data && query.loading && <Busy text="Загружаем сводку…" />}
    {data && <>
      <section className="dashboard-start panel">
        <div className="dash-illustration"><ShoppingBag size={33} strokeWidth={1.6} /></div>
        <div><h2>{empty ? 'Подготовьте первое предложение' : 'Работа магазина'}</h2>
          <p>{empty ? 'Выберите товары из каталога, настройте получение и опубликуйте предложение.' :
            'Проверяйте опубликованные условия и обрабатывайте заказы по мере поступления.'}</p>
          <Link className="button secondary" to={empty ? '/app/offers/new' : '/app/orders'}>{empty ? 'Открыть мастер' : 'Посмотреть заказы'}</Link></div>
      </section>
      <div className="metrics">
        {([
          ['Активные предложения', data.active_offers, FileText],
          ['Опубликованные предложения', data.published_offers, Activity],
          ['Расчёты', data.quotes_created, ChartNoAxesColumnIncreasing],
          ['Активные резервы', data.active_reservations, Clock4],
          ['Создано заказов', data.orders_created, ShoppingBag],
          ['Принято', data.orders_accepted, Box],
          ['Исполнено', data.orders_fulfilled, Box],
          ['Отменено', data.orders_cancelled, RotateCcw],
        ] as const).map(([name, value, Icon]) => <div className="metric panel" key={name}>
          <span className="metric-icon"><Icon size={19} /></span><div><span>{name}</span><b>{value}</b></div>
        </div>)}
      </div>
      <div className="panel summary-note">
        <div><b>Сумма созданных тестовых заказов, включая отменённые</b>
          <strong>{money(data.order_value_minor)}</strong></div>
        <p>Внутренние заказы VK не измеряются. Поколение {data.generation}. Данные на {date(data.as_of)}.</p>
      </div>
    </>}
    {session?.role === 'OWNER' && <Link to="/app/demo" className="button secondary"><UserRoundPlus size={18} />Пригласить участника</Link>}
  </>;
}

export function DemoControls() {
  const { session, busy, pending, mutate, recoveredLink, acknowledgeLink } = useSession();
  const navigate = useScreenNavigation();
  const [links, setLinks] = useState<Partial<Record<'BUYER' | 'OPERATOR', DTO<'RoleLink'>>>>({});
  const [error, setError] = useState<unknown>(null);
  const [confirm, setConfirm] = useState(false);
  const locked = busy || !!pending;
  function linkUrl(value: DTO<'RoleLink'>) {
    const link = sameOrigin(value.url, /^\/join$/);
    if (!link || link.search || !/^#token=[A-Za-z0-9_-]{43}$/.test(link.hash)) {
      throw new Error('Сервер вернул недопустимую ссылку приглашения.');
    }
    return link.href;
  }
  useEffect(() => {
    if (!recoveredLink) return;
    try {
      linkUrl(recoveredLink);
      setLinks(previous => ({ ...previous, [recoveredLink.role]: recoveredLink }));
    } catch (cause) { setError(cause); }
    acknowledgeLink();
  }, [recoveredLink, acknowledgeLink]);
  const makeLink = async (role: 'BUYER' | 'OPERATOR') => {
    setError(null);
    try {
      const answer = await mutate('createRoleLink', { body: { role } });
      linkUrl(answer);
      setLinks(previous => ({ ...previous, [role]: answer }));
    } catch (cause) { setError(cause); }
  };
  async function reset() {
    if (!session) return;
    setError(null);
    try {
      await mutate('resetSandbox', { body: { expected_generation: session.generation } });
      setLinks({});
      setConfirm(false);
      navigate('/app');
    } catch (cause) { setError(cause); setConfirm(false); }
  }
  return <>
    <Title title="Настройки демо" description="Выдавайте отдельные ссылки покупателю и оператору." />
    <Problem error={error} />
    <div className="cols">
      {(['BUYER', 'OPERATOR'] as const).map(role => <section className="panel stack" key={role}>
        <span className="large-icon"><Link2 size={23} /></span>
        <h2>{role === 'BUYER' ? 'Вход покупателя' : 'Вход оператора'}</h2>
        <p>Одноразовая ссылка на 15 минут. Откройте её в отдельном профиле или приватном окне.</p>
        <button className="button primary" disabled={locked} onClick={() => { void makeLink(role); }}>
          {role === 'BUYER' ? 'Создать ссылку покупателя' : 'Создать ссылку оператора'}</button>
        {links[role] && <><Copy value={linkUrl(links[role]!)} />
          <small>Действует до {date(links[role]!.expires_at)}</small></>}
      </section>)}
    </div>
    <section className="panel stack"><h2>Сброс демонстрации</h2>
      <p>Текущее поколение {session?.generation}. После сброса потребуется выдать новые ссылки ролей.</p>
      <button className="button secondary" disabled={locked} onClick={() => setConfirm(true)}>
        <RotateCcw size={17} />Сбросить демонстрацию</button>
    </section>
    {confirm && <ConfirmDialog title="Сбросить демонстрацию?" confirm="Сбросить" disabled={locked}
      approve={() => { void reset(); }} dismiss={() => setConfirm(false)}>
      <p>Восстановится контрольный каталог. Прежние предложения и заказы станут недоступны в новом поколении.
        Ссылки и сессии других ролей потребуется создать заново.</p>
    </ConfirmDialog>}
  </>;
}
