// The one HTTP client for the Phronesis API: bearer-header helper type, error-message extraction, a JSON
// request helper and the server-sent-events reader shared by the chat stream and background-job streams.
//
// Every caller passes the committed backend URL and the page's authHeaders helper, so the bearer token is only
// ever sent to that URL. Imports between services use explicit `.ts` paths so `node --test` can load them.

/** Adds the bearer header (when a token is set) to `extra`. */
export type AuthHeaders = (extra?: Record<string, string>) => Record<string, string>;

/** A failed API call. `status` is the HTTP status, or 0 when no response arrived (network failure). */
export class ApiError extends Error {
  status: number;
  detail: string;
  body: unknown;
  constructor(status: number, message: string, options: { detail?: string; body?: unknown; cause?: unknown } = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "ApiError";
    this.status = status;
    this.detail = options.detail ?? "";
    this.body = options.body ?? null;
  }
}

export const UNREACHABLE_MESSAGE = "The Phronesis backend is unreachable.";

/** The readable message of anything thrown, or `fallback` when it has none. */
export function errorMessage(err: unknown, fallback = ""): string {
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === "string" && err) return err;
  if (err && typeof err === "object" && typeof (err as { message?: unknown }).message === "string") {
    return (err as { message: string }).message || fallback;
  }
  return fallback;
}

/** The HTTP status carried by a thrown error (ApiError or any `{status}`), or 0. */
export function errorStatus(err: unknown): number {
  if (err && typeof err === "object" && typeof (err as { status?: unknown }).status === "number") return (err as { status: number }).status;
  return 0;
}

export function isAbortError(err: unknown): boolean {
  return !!err && typeof err === "object" && (err as { name?: unknown }).name === "AbortError";
}

/**
 * The API's `detail` as text: FastAPI sends a string for HTTPException and a list of `{msg}` objects for
 * request-validation errors (joined with "; ").
 */
export function detailOf(body: unknown): string {
  const b = (body && typeof body === "object" ? body : {}) as { detail?: unknown };
  if (typeof b.detail === "string") return b.detail;
  if (Array.isArray(b.detail)) {
    return b.detail
      .map((d: unknown) => (d && typeof d === "object" && typeof (d as { msg?: unknown }).msg === "string" ? (d as { msg: string }).msg : ""))
      .filter(Boolean)
      .join("; ");
  }
  return "";
}

/**
 * Plain-language message for a failed request.
 * - "brief" (panels, jobs, projects): the server's detail when it has one.
 * - "detailed" (the chat composer): status plus detail plus what the user can do about it.
 */
export function httpErrorMessage(status: number, detail: string, style: "brief" | "detailed" = "brief"): string {
  const d = detail ? `: ${detail}` : "";
  if (style === "detailed") {
    switch (status) {
      case 401:
      case 403:
        return `Not authorized (${status})${d}. Check the access token under Settings → Phronesis Access Token.`;
      case 422:
        return `The request was rejected as invalid (422)${d}.`;
      case 429:
        return `Too many requests (429)${d}. Wait for the running question to finish, then try again.`;
      case 503:
        return `The API is unavailable (503)${d}. If authentication is not configured on the server, set PHRONESIS_API_TOKENS or PHRONESIS_JWT_SECRET (or PHRONESIS_AUTH_MODE=disabled for local development).`;
      default:
        return `The request failed with status ${status}${d}.`;
    }
  }
  switch (status) {
    case 401:
    case 403:
      return `Not authorized (${status}). Check the access token in Settings.`;
    case 404:
      return detail || "Not found (it may have finished or belong to another user).";
    case 409:
      return detail || "The request conflicts with the current state.";
    case 422:
      return `Rejected as invalid${d}.`;
    case 429:
      return detail || "Too many requests; wait for a running question to finish.";
    case 503:
      return detail || "The API is unavailable (503).";
    default:
      return detail || `Request failed (${status}).`;
  }
}

/** Parses a response body as JSON; null for 204, empty or non-JSON bodies. */
export async function readJsonBody(res: Response): Promise<unknown> {
  if (res.status === 204) return null;
  try {
    return await res.json();
  } catch {
    return null;
  }
}

export interface RequestOptions {
  /** Message style for the default error (see httpErrorMessage). */
  style?: "brief" | "detailed";
  /** Builds the error for a non-2xx response instead of the default ApiError. */
  toError?: (status: number, body: unknown, res: Response) => Error;
  /**
   * When true (the default) a network failure becomes ApiError(0, UNREACHABLE_MESSAGE); when false the
   * original error (a fetch TypeError) is rethrown. Aborts are always rethrown unchanged.
   */
  wrapNetworkErrors?: boolean;
  /** Called with the status of every response that arrives (e.g. to note auth problems). */
  onStatus?: (status: number) => void;
}

/** fetch() that resolves only for a 2xx response and throws a readable error otherwise. */
export async function apiFetch(url: string, init: RequestInit = {}, options: RequestOptions = {}): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    if (isAbortError(err) || init.signal?.aborted || options.wrapNetworkErrors === false) throw err;
    throw new ApiError(0, UNREACHABLE_MESSAGE, { cause: err });
  }
  options.onStatus?.(res.status);
  if (!res.ok) {
    const body = await readJsonBody(res);
    if (options.toError) throw options.toError(res.status, body, res);
    const detail = detailOf(body);
    throw new ApiError(res.status, httpErrorMessage(res.status, detail, options.style), { detail, body });
  }
  return res;
}

/** A JSON request: resolves with the parsed body (null for 204 or a non-JSON body). */
export async function requestJson<T>(url: string, init: RequestInit = {}, options: RequestOptions = {}): Promise<T> {
  const res = await apiFetch(url, init, options);
  return (await readJsonBody(res)) as T;
}

/** Init for a JSON body: method, Content-Type (plus the bearer header when authHeaders is given) and body. */
export function jsonInit(method: string, body: unknown, authHeaders?: AuthHeaders, init: RequestInit = {}): RequestInit {
  const base = { "Content-Type": "application/json" };
  return { ...init, method, headers: authHeaders ? authHeaders(base) : base, body: JSON.stringify(body) };
}

// ---------------------------------------------------------------------------------------------------------------
// Server-sent events
// ---------------------------------------------------------------------------------------------------------------

/** No bytes (not even a ": ping" heartbeat, sent every 15 s by the backend) for this long means the stream stalled. */
export const SSE_STALL_MS = 60_000;

export class SseStallError extends Error {
  constructor(ms: number) {
    super(`Connection stalled: nothing arrived from the server for ${Math.round(ms / 1000)} s.`);
    this.name = "SseStallError";
  }
}

export interface SseFrame {
  /** The data lines joined with "\n" ("" for a comment-only frame such as ": ping"). */
  data: string;
  id?: string;
  event?: string;
}

/** Parses one SSE frame (the text between blank lines). Comment lines (": ping") are ignored. */
export function parseSseFrame(raw: string): SseFrame {
  const data: string[] = [];
  const frame: SseFrame = { data: "" };
  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") data.push(value);
    else if (field === "id") frame.id = value;
    else if (field === "event") frame.event = value;
  }
  frame.data = data.join("\n");
  return frame;
}

/**
 * Incremental SSE parser: push() decoded text as it arrives, end() when the stream closes. Frames are split on
 * blank lines; CRLF and CR line endings are normalised; end() flushes a final frame that had no trailing blank
 * line. onFrame returns true to stop; later frames are then ignored.
 */
export function createSseParser(onFrame: (frame: SseFrame) => boolean | void) {
  let buffer = "";
  let stopped = false;
  const emit = (raw: string) => {
    if (stopped || !raw.trim()) return;
    if (onFrame(parseSseFrame(raw))) stopped = true;
  };
  return {
    push(text: string): boolean {
      if (stopped) return true;
      buffer += text;
      // Hold back a trailing "\r" until the next chunk shows whether it is half of "\r\n".
      const held = buffer.endsWith("\r") ? "\r" : "";
      const ready = (held ? buffer.slice(0, -1) : buffer).replace(/\r\n?/g, "\n");
      const frames = ready.split("\n\n");
      buffer = (frames.pop() ?? "") + held;
      for (const raw of frames) {
        emit(raw);
        if (stopped) break;
      }
      return stopped;
    },
    end(): boolean {
      const rest = buffer.replace(/\r\n?/g, "\n");
      buffer = "";
      emit(rest);
      return stopped;
    },
    get stopped() {
      return stopped;
    },
  };
}

export interface SseReadOptions {
  /** Throw SseStallError when no bytes arrive for this long; 0 disables. Defaults to SSE_STALL_MS. */
  stallMs?: number;
}

/**
 * Reads an SSE body and calls onEvent for each JSON data event (with the frame's `id`, the job log's seq).
 * Heartbeat comments count as activity for the stall timer but produce no event; malformed JSON is skipped.
 * onEvent returns true to stop reading (the body is then cancelled); a throw from onEvent propagates.
 */
export async function readSseEvents<T = unknown>(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: T, frame: SseFrame) => boolean | void,
  options: SseReadOptions = {}
): Promise<void> {
  const stallMs = options.stallMs ?? SSE_STALL_MS;
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = createSseParser((frame) => {
    if (!frame.data) return false;
    let parsed: T;
    try {
      parsed = JSON.parse(frame.data) as T;
    } catch {
      console.warn("Skipping malformed stream event", frame.data.slice(0, 200));
      return false;
    }
    return onEvent(parsed, frame) === true;
  });

  const read = (): Promise<ReadableStreamReadResult<Uint8Array>> => {
    if (!(stallMs > 0)) return reader.read();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new SseStallError(stallMs)), stallMs);
      reader.read().then(
        (r) => {
          clearTimeout(timer);
          resolve(r);
        },
        (e) => {
          clearTimeout(timer);
          reject(e);
        }
      );
    });
  };

  let finished = false;
  try {
    for (;;) {
      const { done, value } = await read();
      if (done) {
        finished = true;
        parser.push(decoder.decode());
        parser.end();
        return;
      }
      if (parser.push(decoder.decode(value, { stream: true }))) return;
    }
  } finally {
    if (!finished) {
      // Stopped early, stalled or failed: close the connection instead of leaving it open.
      reader.cancel().catch(() => {});
    }
    try {
      reader.releaseLock();
    } catch {
      // the stream may already be closed
    }
  }
}

/** Resolves after `ms`, or rejects with the abort reason when `signal` aborts first. */
export function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason ?? new DOMException("Aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}
