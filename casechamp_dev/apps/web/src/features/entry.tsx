import { BadgeCheck, Box, KeyRound, PackageCheck, Store, Workflow } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, prepare, read, send } from '../app/api';
import type { DTO, Prepared } from '../app/api';
import { useSession } from '../app/session';
import { useScreenNavigation } from '../app/navigation';
import { Busy, Empty, Problem, Title } from '../ui/common';

type BootOp = 'createSandbox' | 'exchangeRoleLink';

function useBootstrapCommand<N extends BootOp>() {
  const context = useSession();
  const navigate = useScreenNavigation();
  const attempt = useRef<Prepared<N> | null>(null);
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [limited, setLimited] = useState(false);
  const cooldown = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(cooldown.current), []);
  async function run(make: () => Prepared<N>) {
    if (inFlight.current || limited) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    let acknowledged = false;
    try {
      const bootstrap = await read('bootstrapDemo', {});
      attempt.current ??= make();
      const result = await send(attempt.current, bootstrap.csrf_token);
      acknowledged = true;
      // Always check the installed cookie before exposing authorized screens.
      const current = await read('getSession', {});
      if (current.actor_id !== (result.data as DTO<'Session'>).actor_id) throw new Error('Сессия изменилась. Обновите страницу.');
      context.useSession(current);
      navigate(current.role === 'BUYER' ? '/' : '/app', { replace: true });
    } catch (issue) {
      if (!acknowledged && issue instanceof ApiError && issue.status < 500 && issue.code !== 'CSRF_REJECTED') attempt.current = null;
      if (issue instanceof ApiError && issue.status === 429 && issue.details.retry_after_seconds) {
        setLimited(true);
        cooldown.current = window.setTimeout(() => setLimited(false), issue.details.retry_after_seconds * 1000);
      }
      setPending(!!attempt.current);
      setError(issue);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  return { run, busy, limited, error, pending };
}

export function Landing({ buyer }: { buyer: React.ReactNode }) {
  const { session, checking, sessionError, refresh } = useSession();
  const [bundle, setBundle] = useState(false);
  const start = useBootstrapCommand<'createSandbox'>();
  if (checking) return <Busy text="Проверяем сессию…" />;
  if (session?.role === 'BUYER') return buyer;
  return <div className="landing">
    <section className="hero">
      <div className="hero-content">
        <div className="hero-icon"><Store size={26} strokeWidth={1.85} /></div>
        <h1 id="page-title" tabIndex={-1}>Первый заказ<br />из сообщества</h1>
        <p>Соберите товарное предложение, проведите покупателя через резерв и обработайте тестовый заказ.</p>
        {session ? <Link className="button primary" to="/app">Открыть рабочий стол</Link>
          : <form onSubmit={event => {
            event.preventDefault();
            void start.run(() => prepare('createSandbox', { body: { seed: 'CORE-001', enable_bundle: bundle } }));
          }}>
            <button className="button primary" disabled={start.busy || start.limited}>
              {start.busy ? 'Открываем демонстрацию…' : start.pending ? 'Проверить создание' : 'Создать демонстрацию'}
            </button>
            <label className="check hero-check"><input type="checkbox" checked={bundle}
              disabled={start.busy || start.pending} onChange={event => setBundle(event.target.checked)} />
              Добавить демонстрацию подбора</label>
          </form>}
        <Problem error={start.error ?? sessionError} retry={sessionError ? () => { void refresh(); } : undefined} />
      </div>
      <div className="hero-guide">
        <div className="guide-heading"><Workflow size={22} /><strong>Как проходит заказ</strong></div>
        <ol>{[
          [Store, 'Владелец', 'Публикует предложение и выдаёт ссылки ролей.'],
          [Box, 'Покупатель', 'Проверяет условия, резервирует и подтверждает.'],
          [PackageCheck, 'Оператор', 'Принимает и отмечает заказ исполненным.'],
        ].map(([Icon, heading, body], index) => {
          const Symbol = Icon as typeof Store;
          return <li key={index}><span className="guide-icon"><Symbol size={22} /></span>
            <div><b>{String(heading)}</b><span>{String(body)}</span></div></li>;
        })}</ol>
        <span className="guide-footer"><BadgeCheck size={17} />Изолированный тестовый каталог</span>
      </div>
    </section>
  </div>;
}

function removeFragment() {
  const text = new URLSearchParams(window.location.hash.slice(1)).get('token');
  if (window.location.hash) {
    window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search);
  }
  return text && /^[A-Za-z0-9_-]{43}$/.test(text) ? text : null;
}

let openingToken: string | null | undefined;
export function captureInvitation() {
  if (window.location.pathname === '/join') openingToken = removeFragment();
}

export function Join() {
  const [token] = useState(() => {
    if (openingToken !== undefined) {
      const incoming = openingToken;
      openingToken = undefined;
      return incoming;
    }
    return removeFragment();
  });
  const entry = useBootstrapCommand<'exchangeRoleLink'>();
  return <div className="narrow"><Title title="Вход по приглашению" />
    {!token ? <Empty title="Ссылка больше не действует. Владелец демонстрации может создать новую."
      action={<Link className="button secondary" to="/">К началу</Link>} />
      : <section className="panel stack"><div className="large-icon"><KeyRound size={23} /></div>
        <p>Откройте приглашение в отдельном профиле или приватном окне. Ссылка одноразовая.</p>
        <Problem error={entry.error} inLink />
        <div className="actions"><button className="button primary" disabled={entry.busy || entry.limited ||
          (entry.error instanceof ApiError && ['ROLE_LINK_EXPIRED', 'ROLE_LINK_CONSUMED', 'NOT_FOUND', 'SESSION_ALREADY_ACTIVE'].includes(entry.error.code))}
          onClick={() => { void entry.run(() => prepare('exchangeRoleLink', { body: { token } })); }}>
          {entry.busy ? 'Проверяем приглашение…' : entry.pending ? 'Проверить выполнение' : 'Войти по ссылке'}</button>
        <Link className="button secondary" to="/">К началу</Link></div>
      </section>}
  </div>;
}
