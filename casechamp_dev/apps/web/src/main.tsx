import '@fontsource-variable/manrope';
import './ui/style.css';
import { ClipboardList, FileText, LayoutGrid, Settings2, ShoppingBag, Store } from 'lucide-react';
import { useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, Link, NavLink, Outlet, RouterProvider, useLocation, useParams } from 'react-router-dom';
import { label } from './app/format';
import { scopeOf, SessionProvider, useSession } from './app/session';
import { BuyerOffer, QuotePage, ReservationPage } from './features/buyerCheckout';
import { Catalog } from './features/catalog';
import { Dashboard, DemoControls } from './features/controls';
import { captureInvitation, Join, Landing } from './features/entry';
import { EditOffer, NewOffer, OfferDetail, OfferPreview, Offers } from './features/merchantOffers';
import { OrderPage, Orders } from './features/merchantOrders';
import { Busy, Empty, Inaccessible, Problem, Success, Title } from './ui/common';

const navigation = [
  { title: 'Рабочий стол', to: '/app', icon: LayoutGrid },
  { title: 'Каталог', to: '/app/catalog', icon: Store },
  { title: 'Предложения', to: '/app/offers', icon: FileText },
  { title: 'Заказы', to: '/app/orders', icon: ClipboardList },
  { title: 'Настройки демо', to: '/app/demo', icon: Settings2, owner: true },
];

function App() {
  const { session, pending, busy, recover, recoveryError, recovered } = useSession();
  const { pathname } = useLocation();
  const seller = session && session.role !== 'BUYER';
  const sidebar = seller && (pathname === '/app' || pathname.startsWith('/app/'));
  useEffect(() => {
    if (document.activeElement !== document.body) document.getElementById('content')?.focus();
    const content = document.getElementById('content');
    if (!content) return;
    const align = () => {
      const heading = content.querySelector<HTMLElement>('h1');
      if (!heading) return false;
      const active = document.activeElement;
      if (!content.contains(active) || active === content || active === document.body) heading.focus({ preventScroll: true });
      document.title = heading.textContent + ' · CaseChamp';
      return true;
    };
    if (align()) return;
    const observer = new MutationObserver(() => { if (align()) observer.disconnect(); });
    observer.observe(content, { subtree: true, childList: true });
    return () => observer.disconnect();
  }, [pathname]);
  return <>
    <a href="#content" className="skip">К содержимому</a>
    <header className="topbar">
      <div className="topbar-inner">
        <Link className="brand" to={seller ? '/app' : '/'}><span className="brand-mark"><ShoppingBag size={21} /></span>
          <span>CaseChamp <small>Первый заказ</small></span></Link>
        <div className="top-status">
          <span className="demo-badge"><i />Демонстрация · тестовые заказы</span>
          {session && <span className="identity"><b>{label(session.role)}</b><span>Демо {session.sandbox_id.slice(0, 8)}</span></span>}
        </div>
      </div>
    </header>
    <div className={sidebar ? 'shell with-sidebar' : 'shell'}>
      {sidebar && <aside className="sidebar">
        <nav aria-label="Рабочий стол продавца">{navigation.filter(item => !item.owner || session.role === 'OWNER')
          .map(({ title, to, icon: Icon }) => <NavLink key={to} to={to} end={to === '/app'}><Icon size={20} aria-hidden="true" />{title}</NavLink>)}</nav>
      </aside>}
      <main id="content" className="content stack" tabIndex={-1}>
        {pending && <section className="notice info recovery" role="status"><div>
          <b>{busy ? 'Проверяем результат действия' : 'Результат ещё не получен.'}</b>
          {!busy && <p>Сверьте выполнение исходной команды перед новым действием.</p>}
          <button className="button secondary" disabled={busy} onClick={() => void recover()}>Проверить выполнение</button>
        </div></section>}
        <Problem error={recoveryError} />
        {recovered && <Success>Результат действия проверен.</Success>}
        <Outlet key={pathname === '/join' ? 'invitation' : session ? scopeOf(session) : 'guest'} />
      </main>
    </div>
  </>;
}

function Protected({ role }: { role: 'seller' | 'owner' | 'buyer' }) {
  const { session, checking, sessionError, refresh } = useSession();
  if (checking) return <Busy text="Проверяем сессию…" />;
  if (!session) return <>
    <Title title="Вход в демонстрацию" />
    <Problem error={sessionError} retry={refresh} />
    <Empty title={role === 'buyer' ? 'Нужна роль покупателя' : 'Нужна сессия продавца'}
      text={role === 'buyer' ? 'Попросите у владельца отдельную ссылку покупателя, затем откройте предложение в этом же профиле.'
        : 'Создайте демонстрацию или откройте ссылку роли в отдельном профиле.'}
      action={<Link className="button primary" to="/">К началу</Link>} />
  </>;
  if (role === 'buyer' && session.role !== 'BUYER' || role === 'seller' && session.role === 'BUYER' ||
    role === 'owner' && session.role !== 'OWNER') return <Inaccessible />;
  return <Outlet />;
}

function ValidId() {
  const id = useParams().id;
  return id && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id)
    ? <Outlet /> : <Inaccessible />;
}

captureInvitation();
const router = createBrowserRouter([{
  element: <SessionProvider><App /></SessionProvider>,
  children: [
    { index: true, element: <Landing buyer={<Orders buyer />} /> },
    { path: 'join', element: <Join /> },
    { path: 'app', element: <Protected role="seller" />, children: [
      { index: true, element: <Dashboard /> },
      { path: 'catalog', element: <Catalog /> },
      { path: 'offers', element: <Offers /> },
      { path: 'offers/new', element: <NewOffer /> },
      { path: 'offers/:id', element: <ValidId />, children: [
        { index: true, element: <OfferDetail /> },
        { path: 'edit', element: <EditOffer /> }, { path: 'preview', element: <OfferPreview /> },
      ] },
      { path: 'orders', element: <Orders /> },
      { path: 'orders/:id', element: <ValidId />, children: [{ index: true, element: <OrderPage /> }] },
      { element: <Protected role="owner" />, children: [{ path: 'demo', element: <DemoControls /> }] },
    ] },
    { path: 'buy', element: <Protected role="buyer" />, children: [
      { path: ':id', element: <ValidId />, children: [{ index: true, element: <BuyerOffer /> }] },
      { path: 'quotes/:id', element: <ValidId />, children: [{ index: true, element: <QuotePage /> }] },
      { path: 'reservations/:id', element: <ValidId />, children: [{ index: true, element: <ReservationPage /> }] },
      { path: 'orders/:id', element: <ValidId />, children: [{ index: true, element: <OrderPage buyer /> }] },
    ] },
    { path: '*', element: <Inaccessible /> },
  ],
}]);

createRoot(document.getElementById('root')!).render(<RouterProvider router={router} />);
