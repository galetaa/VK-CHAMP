import Ajv2020 from 'ajv/dist/2020';
import addFormats from 'ajv-formats';
import type { components, operations } from '../generated/contract';
import { basePath, endpoints } from '../generated/operations';
import schemas from '../generated/schemas.json';

export type DTO<Name extends keyof components['schemas']> = components['schemas'][Name];
export type Operation = keyof operations;
type Unwrap<P> = P extends { content: { 'application/json': infer T } } ? T : never;
type ResponseFor<N extends Operation> = operations[N]['responses'];
type Success<N extends Operation> = Unwrap<ResponseFor<N>[keyof ResponseFor<N> & (200 | 201)]>;
export type Data<N extends Operation> = Success<N> extends { data: infer T } ? T : never;
type Body<N extends Operation> = operations[N] extends { requestBody: infer R } ? Unwrap<R> : never;
type Parameters<N extends Operation> = operations[N]['parameters'];
type PathFields<N extends Operation> = Parameters<N> extends { path: infer P } ? P : never;
type QueryFields<N extends Operation> = Parameters<N> extends { query?: infer Q } ? NonNullable<Q> : never;
export type Input<N extends Operation> =
  ([Body<N>] extends [never] ? { body?: never } : { body: Body<N> }) &
  ([PathFields<N>] extends [never] ? { path?: never } : { path: PathFields<N> }) &
  ([QueryFields<N>] extends [never] ? { query?: never } : { query?: QueryFields<N> });

type BareInput = { body?: unknown; path?: unknown; query?: unknown };
export type Prepared<N extends Operation = Operation> = {
  op: N; method: string; url: string; body?: string; key?: string;
};
export type Envelope<T> = { data: T; meta: DTO<'Meta'> };

const ajv = new Ajv2020({ strict: false, allErrors: false });
addFormats(ajv);
ajv.addSchema(schemas);
const valid = (name: string, value: unknown) => ajv.validate(schemas.$id + '#/$defs/' + name, value);

export class ApiError extends Error {
  constructor(readonly payload: DTO<'ErrorEnvelope'>, readonly status: number) {
    super(payload.error.message);
  }

  get code() { return this.payload.error.code; }
  get details() { return this.payload.error.details; }
}

export class UnknownResult extends Error {
  constructor(readonly kind: 'transport' | 'schema', readonly requestId?: string) {
    super(kind === 'schema' ? 'Некорректная структура ответа сервера' : 'Ответ сервера не получен');
  }
}

type Instant = { value: number; tick: number };
let server: Instant | undefined;
export const serverNow = () => server ? server.value + performance.now() - server.tick : Date.now();
function observeServer(meta: DTO<'Meta'>) {
  const time = Date.parse(meta.server_now);
  if (!server || time >= server.value) server = { value: time, tick: performance.now() };
}

export function prepare<N extends Operation>(op: N, options: Input<N>): Prepared<N> {
  const values: BareInput = options;
  const definition = endpoints[op];
  let path: string = definition.route;
  for (const [key, value] of Object.entries((values.path ?? {}) as object)) {
    path = path.replace('{' + key + '}', encodeURIComponent(String(value)));
  }
  if (path.includes('{')) throw new Error('Не заполнен параметр маршрута');
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries((values.query ?? {}) as object)) {
    if (value !== undefined) query.set(key, String(value));
  }
  return {
    op, method: definition.method, url: basePath + path + (query.size ? '?' + query : ''),
    ...(definition.idempotent ? { key: crypto.randomUUID() } : {}),
    ...(values.body === undefined ? {} : { body: JSON.stringify(values.body) }),
  };
}

export async function send<N extends Operation>(
  intent: Prepared<N>, csrf?: string, signal?: AbortSignal,
): Promise<Envelope<Data<N>>> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (intent.body !== undefined) headers['Content-Type'] = 'application/json';
  if (csrf) headers['X-CSRF-Token'] = csrf;
  if (intent.key) headers['Idempotency-Key'] = intent.key;
  const timeout = AbortSignal.timeout(10_000);
  let response: Response;
  try {
    response = await fetch(intent.url, {
      method: intent.method, headers, body: intent.body, credentials: 'same-origin',
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new UnknownResult('transport');
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch (error) {
    if (signal?.aborted) throw error;
    if (error instanceof SyntaxError) throw new UnknownResult('schema', response.headers.get('X-Request-ID') ?? undefined);
    throw new UnknownResult('transport');
  }
  if (!response.ok && valid('ErrorEnvelope', body)) {
    const error = body as DTO<'ErrorEnvelope'>;
    observeServer(error.meta);
    throw new ApiError(error, response.status);
  }
  if (response.status !== endpoints[intent.op].status || !valid(endpoints[intent.op].envelope, body)) {
    throw new UnknownResult('schema', response.headers.get('X-Request-ID') ?? undefined);
  }
  const reply = body as Envelope<Data<N>>;
  observeServer(reply.meta);
  return reply;
}

const pause = (milliseconds: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  if (signal?.aborted) {
    reject(signal.reason);
    return;
  }
  const stop = () => { clearTimeout(timer); reject(signal?.reason); };
  const timer = setTimeout(() => { signal?.removeEventListener('abort', stop); resolve(); }, milliseconds);
  signal?.addEventListener('abort', stop, { once: true });
});

export async function read<N extends Operation>(
  name: N, options: Input<N>, signal?: AbortSignal,
): Promise<Data<N>> {
  const request = prepare(name, options);
  if (request.method !== 'GET') throw new Error('Для изменения требуется отдельное намерение');
  for (const delay of [300, 1000, null]) {
    try {
      return (await send(request, undefined, signal)).data;
    } catch (error) {
      const temporary = (error instanceof UnknownResult && error.kind === 'transport')
        || (error instanceof ApiError && error.status === 503);
      if (signal?.aborted || !temporary || delay === null) throw error;
      await pause(delay, signal);
    }
  }
  throw new Error('Повторы чтения исчерпаны');
}
