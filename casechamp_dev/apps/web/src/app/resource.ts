import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { DTO, Data, Input, Operation } from './api';
import { ApiError, read, serverNow } from './api';
import { scopeOf, useSession } from './session';

type Reading<T> = { key: string; data?: T; error?: unknown; loading: boolean };
export function useResource<N extends Operation>(
  name: N, input: Input<N>, optional: {
    enabled?: boolean; interval?: number; when?: (data: Data<N>) => boolean;
    refreshOnFocus?: boolean;
  } = {},
) {
  const { session, revision, refresh } = useSession();
  const encoded = JSON.stringify(input);
  const key = (session ? scopeOf(session) : '') + ':' + name + ':' + encoded;
  const options = useMemo(() => JSON.parse(encoded) as Input<N>, [encoded]);
  const [state, setState] = useState<Reading<Data<N>>>({ key: '', loading: true });
  const active = useRef<AbortController | null>(null);
  const seq = useRef(0);
  const usable = !!session && optional.enabled !== false;
  const reload = useCallback(async () => {
    if (!usable) return;
    const generation = ++seq.current;
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setState(previous => ({ ...(previous.key === key ? previous : {}), key, error: undefined, loading: true }));
    try {
      const value = await read(name, options, controller.signal);
      if (generation === seq.current) setState({ key, data: value, loading: false });
      return value;
    } catch (error) {
      if (generation === seq.current && !controller.signal.aborted) {
        setState({ key, error, loading: false });
        if (error instanceof ApiError && (error.status === 401 || error.code === 'SANDBOX_EXPIRED')) void refresh();
      }
      return undefined;
    }
  }, [name, options, usable, key, refresh]);

  useEffect(() => {
    void reload();
    return () => { seq.current++; active.current?.abort(); };
  }, [reload, revision]);

  const data = state.key === key && usable ? state.data : undefined;
  const poll = optional.interval && data && (optional.when?.(data) ?? true);
  useEffect(() => {
    if (!poll) return;
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible' && !state.loading) void reload();
    }, optional.interval);
    return () => clearInterval(timer);
  }, [poll, optional.interval, reload, state.loading]);
  useEffect(() => {
    if (optional.refreshOnFocus === false || !usable) return;
    const focus = () => {
      if (document.visibilityState === 'visible') void reload();
    };
    document.addEventListener('visibilitychange', focus);
    window.addEventListener('focus', focus);
    return () => {
      document.removeEventListener('visibilitychange', focus);
      window.removeEventListener('focus', focus);
    };
  }, [reload, optional.refreshOnFocus, usable]);
  return { data, error: state.key === key && usable ? state.error : undefined,
    loading: usable && (state.key !== key || state.loading), reload };
}

export function useCountdown(expiry: string | undefined, onExpire?: () => void) {
  const calc = useCallback(() => expiry ? Math.max(0, Math.ceil((Date.parse(expiry) - serverNow()) / 1000)) : 0, [expiry]);
  const [remaining, setRemaining] = useState(calc);
  const fired = useRef<string | undefined>(undefined);
  const callback = useRef(onExpire);
  callback.current = onExpire;
  useEffect(() => {
    const update = () => {
      const next = calc();
      setRemaining(next);
      if (next === 0 && expiry && fired.current !== expiry) {
        fired.current = expiry;
        callback.current?.();
      }
    };
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [calc, expiry]);
  return expiry ? Math.min(remaining, calc()) : 0;
}

export const liveOrder = (order: DTO<'Order'>) => order.status === 'CREATED' || order.status === 'ACCEPTED';
