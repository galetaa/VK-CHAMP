import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from 'react';
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError, UnknownResult, prepare, read, send } from './api';
import type { DTO, Data, Input, Operation, Prepared } from './api';
import { basePath, endpoints } from '../generated/operations';

type Session = DTO<'Session'>;
type Pending = { scope: string; intent: Prepared };
const storageKey = 'casechamp-v2-pending';
export const reservationStorageKey = 'casechamp-v2-recent-reservation';
const keep = new Set<Operation>([
  'createSku', 'updateSku', 'createOffer', 'updateOfferDraft', 'publishOffer', 'pauseOffer',
  'archiveOffer', 'createQuote', 'createReservation', 'cancelReservation', 'confirmReservation',
  'transitionOrder', 'resetSandbox', 'solveBundle',
]);
export const scopeOf = (session: Session) =>
  [session.sandbox_id, session.generation, session.actor_id].join(':');

function matches(pending: Pending | null, session: Session) {
  if (pending?.scope === scopeOf(session)) return true;
  // The owner receipt survives a successful reset, including a response lost before reload.
  return pending?.intent.op === 'resetSandbox' && session.role === 'OWNER' && session.generation > 1 &&
    pending.scope === scopeOf({ ...session, generation: session.generation - 1 }) &&
    pending.intent.body === JSON.stringify({ expected_generation: session.generation - 1 });
}

function load(session: Session) {
  try {
    const pending: Pending | null = JSON.parse(sessionStorage.getItem(storageKey) ?? 'null');
    const intent = pending?.intent;
    if (matches(pending, session) && intent && keep.has(intent.op) && typeof intent.key === 'string') {
      const shape = endpoints[intent.op];
      const pattern = new RegExp('^' + basePath + shape.route.replace(/\{[^}]+\}/g, '[A-Za-z0-9_-]+') + '$');
      if (intent.method === shape.method && pattern.test(intent.url) && typeof intent.body === 'string') return pending;
    }
    sessionStorage.removeItem(storageKey);
  } catch { /* Storage can be disabled by the browser. */ }
  return null;
}

function store(pending: Pending | null) {
  try {
    if (pending && keep.has(pending.intent.op)) sessionStorage.setItem(storageKey, JSON.stringify(pending));
    else sessionStorage.removeItem(storageKey);
  } catch { /* The current tab still has its pending intent in memory. */ }
}

type State = {
  session: Session | null; checking: boolean; sessionError: unknown;
  refresh: () => Promise<Session | null>; useSession: (session: Session) => void;
  pending: Pending | null; busy: boolean; recoveryError: unknown; recovered: boolean;
  recoveredLink: DTO<'RoleLink'> | null; acknowledgeLink: () => void;
  recover: () => Promise<void>; revision: number; invalidate: () => void;
  mutate: <N extends Operation>(op: N, options: Input<N>) => Promise<Data<N>>;
  event: (name: DTO<'UiEventRequest'>['name'], context?: {
    offerId: string; version: number;
  }, step?: DTO<'UiEventRequest'>['ui_step']) => void;
};
const Context = createContext<State | null>(null);
export function useSession() {
  const context = useContext(Context);
  if (!context) throw new Error('SessionProvider is required');
  return context;
}

async function currentEffect(op: Operation, value: unknown): Promise<unknown> {
  const data = value as { id: string; sku?: string };
  switch (op) {
    case 'createOffer': case 'updateOfferDraft': case 'publishOffer': case 'archiveOffer': case 'pauseOffer':
      return read('getOffer', { path: { offer_id: data.id } });
    case 'createQuote': return read('getQuote', { path: { quote_id: data.id } });
    case 'createReservation': case 'cancelReservation':
      return read('getReservation', { path: { reservation_id: data.id } });
    case 'confirmReservation': case 'transitionOrder':
      return read('getOrder', { path: { order_id: data.id } });
    case 'solveBundle': return read('getBundleSolution', { path: { solution_id: data.id } });
    case 'createSku': case 'updateSku': {
      if (!data.sku) return value;
      let cursor: string | null = null;
      do {
        const page: DTO<'CatalogPage'> = await read('listCatalog', { query: { limit: 100, cursor: cursor ?? undefined } });
        const item = page.items.find(candidate => candidate.sku === data.sku);
        if (item) return item;
        cursor = page.next_cursor;
      } while (cursor);
      return value;
    }
    default: return value;
  }
}

function locationFrom(op: Operation, value: unknown, role: Session['role']) {
  const data = value as { id: string; offer_id?: string; order_id?: string | null };
  switch (op) {
    case 'createOffer': case 'updateOfferDraft': return '/app/offers/' + data.id + '/edit';
    case 'publishOffer': case 'archiveOffer': case 'pauseOffer': return '/app/offers/' + data.id;
    case 'createQuote': return '/buy/quotes/' + data.id;
    case 'createReservation': case 'cancelReservation':
      return data.order_id ? '/buy/orders/' + data.order_id : '/buy/reservations/' + data.id;
    case 'confirmReservation': case 'transitionOrder':
      return (role === 'BUYER' ? '/buy/orders/' : '/app/orders/') + data.id;
    case 'solveBundle': return '/buy/' + data.offer_id + '?solution=' + data.id;
    case 'resetSandbox': return '/app';
    case 'createSku': case 'updateSku': return '/app/catalog';
    default: return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [sessionError, setSessionError] = useState<unknown>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [recovered, setRecovered] = useState(false);
  const [recoveredLink, setRecoveredLink] = useState<DTO<'RoleLink'> | null>(null);
  const [recoveryError, setRecoveryError] = useState<unknown>(null);
  const [limited, setLimited] = useState(false);
  const limitedUntil = useRef(0);
  const cooldown = useRef<number | undefined>(undefined);
  const live = useRef<Session | null>(null);
  const held = useRef<Pending | null>(null);
  const running = useRef(false);
  const reading = useRef(0);
  const joining = useRef(true);
  const invalidate = useCallback(() => setRevision(n => n + 1), []);
  const acknowledgeLink = useCallback(() => setRecoveredLink(null), []);

  const retain = useCallback((attempt: Pending | null) => {
    held.current = attempt;
    setPending(attempt);
    store(attempt);
  }, []);

  const applySession = useCallback((next: Session | null, startup = false) => {
    const previous = live.current;
    live.current = next;
    setSession(next);
    if (startup && next) retain(load(next));
    else if (!next || !previous || scopeOf(next) !== scopeOf(previous)) {
      if (!next || !matches(held.current, next)) retain(null);
      acknowledgeLink();
      limitedUntil.current = 0;
      window.clearTimeout(cooldown.current);
      setLimited(false);
      try { sessionStorage.removeItem(reservationStorageKey); } catch { /* Optional storage. */ }
      invalidate();
    }
  }, [acknowledgeLink, invalidate, retain]);

  const acceptSession = useCallback((next: Session) => {
    reading.current++;
    joining.current = false;
    applySession(next);
    setSessionError(null);
    setChecking(false);
  }, [applySession]);

  const refresh = useCallback(async () => {
    const serial = ++reading.current;
    try {
      const current = await read('getSession', {});
      if (serial !== reading.current) return live.current;
      applySession(current, joining.current);
      setSessionError(null);
      return current;
    } catch (error) {
      if (serial !== reading.current) return live.current;
      const access = error instanceof ApiError && (error.status === 401 || error.code === 'SANDBOX_EXPIRED');
      if (access) applySession(null);
      setSessionError(access && error.code === 'AUTH_REQUIRED' ? null : error);
      return live.current;
    } finally {
      if (serial === reading.current) {
        joining.current = false;
        setChecking(false);
      }
    }
  }, [applySession]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => () => window.clearTimeout(cooldown.current), []);
  useEffect(() => {
    const regain = () => { if (document.visibilityState === 'visible') void refresh(); };
    window.addEventListener('focus', regain);
    document.addEventListener('visibilitychange', regain);
    return () => {
      window.removeEventListener('focus', regain);
      document.removeEventListener('visibilitychange', regain);
    };
  }, [refresh]);

  const execute = useCallback(async <N extends Operation>(
    intent: Prepared<N>, origin: Session, verifying: boolean,
  ) => {
    if (running.current) throw new Error('Команда уже выполняется');
    running.current = true;
    setBusy(true);
    setRecovered(false);
    setRecoveryError(null);
    const stamp = scopeOf(origin);
    try {
      const answer = await send(intent, origin.csrf_token);
      if (!live.current || scopeOf(live.current) !== stamp) {
        throw new UnknownResult('transport');
      }
      let value = answer.data;
      if (intent.op === 'resetSandbox') {
        const next = await refresh();
        if (!next || next.generation < (value as DTO<'ResetResult'>).generation) {
          throw new UnknownResult('transport');
        }
      } else if (verifying || answer.meta.replayed) {
        value = await currentEffect(intent.op, value) as Data<N>;
      }
      if (held.current?.intent.key === intent.key) retain(null);
      invalidate();
      return value;
    } catch (error) {
      if (error instanceof ApiError && error.code === 'CSRF_REJECTED') {
        await refresh();
      } else if (error instanceof ApiError && error.status < 500) {
        if (held.current?.intent.key === intent.key) retain(null);
        if (error.status === 401 || error.code === 'SANDBOX_EXPIRED') await refresh();
        if (error.status === 429 && error.details.retry_after_seconds) {
          const wait = error.details.retry_after_seconds * 1000;
          limitedUntil.current = performance.now() + wait;
          setLimited(true);
          window.clearTimeout(cooldown.current);
          cooldown.current = window.setTimeout(() => setLimited(false), wait);
        }
      }
      throw error;
    } finally {
      running.current = false;
      setBusy(false);
    }
  }, [invalidate, refresh, retain]);

  const mutate: State['mutate'] = useCallback(async (op, options) => {
    const actor = live.current;
    if (!actor || running.current || held.current) throw new Error('Сначала завершите проверку предыдущего действия');
    if (performance.now() < limitedUntil.current) throw new Error('Дождитесь окончания ограничения запросов.');
    const attempt = prepare(op, options);
    // Preview and UI events have no idempotency key and no commercial effect.
    if (attempt.key) retain({ scope: scopeOf(actor), intent: attempt });
    try {
      return await execute(attempt, actor, false);
    } catch (error) {
      // A verified computation failure is a known unsuccessful solve.
      if (op === 'solveBundle' && error instanceof ApiError && error.code === 'COMPUTATION_UNAVAILABLE') {
        retain(null);
      }
      throw error;
    }
  }, [execute, retain]);

  const recover = useCallback(async () => {
    const actor = live.current;
    const previous = held.current;
    if (running.current || performance.now() < limitedUntil.current || !previous || !actor || !matches(previous, actor)) return;
    try {
      const value = await execute(previous.intent, actor, true);
      const path = locationFrom(previous.intent.op, value, actor.role);
      if (path) navigate(path);
      if (previous.intent.op === 'createRoleLink') {
        setRecoveredLink(value as DTO<'RoleLink'>);
        navigate('/app/demo');
      }
      setRecovered(true);
    } catch (error) {
      setRecoveryError(error);
    }
  }, [execute, navigate]);

  const event: State['event'] = useCallback((name, data, step = null) => {
    const actor = live.current;
    if (!actor) return;
    void send(prepare('recordUiEvent', { body: {
      event_id: crypto.randomUUID(), name, offer_id: data?.offerId ?? null,
      offer_version_no: data?.version ?? null, ui_step: step,
    } }), actor.csrf_token).catch(() => { /* Analytics never blocks an action. */ });
  }, []);

  const value = useMemo(() => ({
    session, checking, sessionError, refresh, useSession: acceptSession, pending, busy: busy || limited,
    recoveryError, recovered, recoveredLink, acknowledgeLink, recover, revision, invalidate, mutate, event,
  }), [session, checking, sessionError, refresh, acceptSession, pending, busy, limited, recoveryError,
    recovered, recoveredLink, acknowledgeLink, recover, revision, invalidate, mutate, event]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
