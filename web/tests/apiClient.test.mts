// Run with: node --test web/tests/apiClient.test.mts   (Node 24 strips the TypeScript types)
// The shared API client: error-message extraction, the JSON helper and the SSE reader (frame flush,
// heartbeats, multi-line data, stall timer), plus job-stream resume from the last seen seq.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as api from "../src/services/apiClient.ts";
import * as agent from "../src/services/agentService.ts";

const {
  ApiError,
  SseStallError,
  apiFetch,
  createSseParser,
  detailOf,
  errorMessage,
  errorStatus,
  httpErrorMessage,
  parseSseFrame,
  readSseEvents,
  requestJson,
} = api;
const { streamJobEvents } = agent;

const enc = new TextEncoder();

/** A body that delivers one chunk per read; after the last chunk it closes, or errors with `fail`. */
function chunked(chunks: string[], fail?: Error): ReadableStream<Uint8Array> {
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) controller.enqueue(enc.encode(chunks[i++]));
      else if (fail) controller.error(fail);
      else controller.close();
    },
  });
}

/** A body that sends `chunks` with `gapMs` between them, then closes (or stays open when `hang`). */
function timed(chunks: string[], gapMs: number, hang = false): ReadableStream<Uint8Array> {
  let i = 0;
  return new ReadableStream({
    async pull(controller) {
      if (i < chunks.length) {
        await new Promise((r) => setTimeout(r, gapMs));
        controller.enqueue(enc.encode(chunks[i++]));
      } else if (!hang) controller.close();
      else await new Promise(() => {}); // never resolves: a silent connection
    },
  });
}

async function collect(body: ReadableStream<Uint8Array>, opts: { stallMs?: number } = {}): Promise<any[]> {
  const seen: any[] = [];
  await readSseEvents(body, (ev: any) => {
    seen.push(ev);
  }, opts);
  return seen;
}

function withFetch(impl: (url: string, init: any) => Promise<Response>, run: () => Promise<void>): Promise<void> {
  const orig = globalThis.fetch;
  globalThis.fetch = impl as typeof fetch;
  return run().finally(() => {
    globalThis.fetch = orig;
  });
}

const jsonResponse = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

// ---------------------------------------------------------------------------------------------------------------
// Error extraction
// ---------------------------------------------------------------------------------------------------------------

test("detailOf reads a string detail or joins FastAPI validation messages", () => {
  assert.equal(detailOf({ detail: "Job not found" }), "Job not found");
  assert.equal(detailOf({ detail: [{ msg: "field required" }, { loc: ["x"] }, { msg: "too long" }] }), "field required; too long");
  assert.equal(detailOf({ error: "x" }), "");
  assert.equal(detailOf(null), "");
  assert.equal(detailOf("text"), "");
});

test("httpErrorMessage keeps the brief (panels) and detailed (composer) wording", () => {
  assert.equal(httpErrorMessage(401, "bad token"), "Not authorized (401). Check the access token in Settings.");
  assert.equal(httpErrorMessage(404, ""), "Not found (it may have finished or belong to another user).");
  assert.equal(httpErrorMessage(404, "Job not found"), "Job not found");
  assert.equal(httpErrorMessage(422, "message: too long"), "Rejected as invalid: message: too long.");
  assert.equal(httpErrorMessage(500, ""), "Request failed (500).");
  assert.equal(
    httpErrorMessage(403, "expired", "detailed"),
    "Not authorized (403): expired. Check the access token under Settings → Phronesis Access Token."
  );
  assert.equal(httpErrorMessage(429, "", "detailed"), "Too many requests (429). Wait for the running question to finish, then try again.");
  assert.match(httpErrorMessage(503, "", "detailed"), /^The API is unavailable \(503\)\. If authentication is not configured/);
  assert.equal(httpErrorMessage(500, "boom", "detailed"), "The request failed with status 500: boom.");
});

test("errorMessage and errorStatus read anything thrown", () => {
  assert.equal(errorMessage(new Error("x")), "x");
  assert.equal(errorMessage("plain"), "plain");
  assert.equal(errorMessage({ message: "obj" }), "obj");
  assert.equal(errorMessage(undefined, "fallback"), "fallback");
  assert.equal(errorMessage(new Error(""), "fallback"), "fallback");
  assert.equal(errorStatus(new ApiError(409, "conflict")), 409);
  assert.equal(errorStatus({ status: 401 }), 401);
  assert.equal(errorStatus(new Error("x")), 0);
});

test("apiFetch throws ApiError with status, detail and message, and reports every status", async () => {
  const statuses: number[] = [];
  await withFetch(
    async () => jsonResponse(422, { detail: [{ msg: "message too long" }] }),
    async () => {
      await assert.rejects(
        apiFetch("http://x/api/chat", {}, { style: "detailed", onStatus: (s) => statuses.push(s) }),
        (err: any) => {
          assert.ok(err instanceof ApiError);
          assert.equal(err.status, 422);
          assert.equal(err.detail, "message too long");
          assert.equal(err.message, "The request was rejected as invalid (422): message too long.");
          return true;
        }
      );
    }
  );
  assert.deepEqual(statuses, [422]);
});

test("apiFetch wraps network failures unless asked not to, and never wraps aborts", async () => {
  const offline = async () => {
    throw new TypeError("Failed to fetch");
  };
  await withFetch(offline, async () => {
    await assert.rejects(apiFetch("http://x"), (err: any) => err instanceof ApiError && err.status === 0 && err.cause instanceof TypeError);
    await assert.rejects(apiFetch("http://x", {}, { wrapNetworkErrors: false }), (err: any) => err instanceof TypeError && err.message === "Failed to fetch");
  });
  await withFetch(
    async () => {
      throw new DOMException("The operation was aborted.", "AbortError");
    },
    async () => {
      await assert.rejects(apiFetch("http://x"), (err: any) => err.name === "AbortError");
    }
  );
});

test("apiFetch uses a custom error builder; requestJson returns null for 204 and non-JSON bodies", async () => {
  await withFetch(
    async () => jsonResponse(413, { detail: "too big", reason_code: "x" }),
    async () => {
      await assert.rejects(
        apiFetch("http://x", {}, { toError: (status, body: any) => new Error(`${status}:${body.reason_code}`) }),
        /^Error: 413:x$/
      );
    }
  );
  await withFetch(async () => new Response(null, { status: 204 }), async () => {
    assert.equal(await requestJson("http://x"), null);
  });
  await withFetch(async () => new Response("<html>", { status: 200 }), async () => {
    assert.equal(await requestJson("http://x"), null);
  });
  await withFetch(async () => jsonResponse(200, { ok: 1 }), async () => {
    assert.deepEqual(await requestJson("http://x"), { ok: 1 });
  });
});

// ---------------------------------------------------------------------------------------------------------------
// SSE parsing
// ---------------------------------------------------------------------------------------------------------------

test("parseSseFrame joins multi-line data, reads id/event and ignores comments", () => {
  assert.deepEqual(parseSseFrame(": ping"), { data: "" });
  assert.deepEqual(parseSseFrame("id: 7\nevent: message\ndata: {\"a\":\ndata:  1}\n: note"), { data: '{"a":\n 1}', id: "7", event: "message" });
  assert.deepEqual(parseSseFrame("data"), { data: "" });
});

test("createSseParser handles CRLF, a CR/LF split across chunks, and flushes the last frame on end()", () => {
  const frames: string[] = [];
  const p = createSseParser((f) => {
    frames.push(f.data);
  });
  p.push("data: one\r\n\r");
  p.push("\ndata: two\r");
  p.push("\n\r\ndata: three");
  assert.deepEqual(frames, ["one", "two"]);
  p.end();
  assert.deepEqual(frames, ["one", "two", "three"]);
});

test("readSseEvents flushes a final frame that has no trailing blank line", async () => {
  const seen = await collect(chunked(['data: {"type":"content_delta","delta":"a"}\n\n', 'data: {"type":"done","result":{"response":"ok"}}']));
  assert.deepEqual(seen.map((e) => e.type), ["content_delta", "done"]);
  assert.equal(seen[1].result.response, "ok");
});

test("readSseEvents decodes a multi-byte character split across chunks", async () => {
  const bytes = enc.encode('data: {"delta":"β-catenin"}\n\n');
  const cut = bytes.indexOf(0xce) + 1; // inside the two-byte "β"
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(bytes.slice(0, cut));
      c.enqueue(bytes.slice(cut));
      c.close();
    },
  });
  const seen = await collect(body);
  assert.equal(seen[0].delta, "β-catenin");
});

test("readSseEvents skips heartbeats and malformed data, and parses multi-line JSON", async () => {
  const warn = console.warn;
  console.warn = () => {};
  try {
    const seen = await collect(
      chunked([": ping\n\n", "data: not-json\n\n", 'data: {"type":"thought",\ndata: "data":{"id":"s1"}}\n\n', ": ping\n\n"])
    );
    assert.equal(seen.length, 1);
    assert.equal(seen[0].type, "thought");
    assert.equal(seen[0].data.id, "s1");
  } finally {
    console.warn = warn;
  }
});

test("readSseEvents stops when onEvent returns true and passes the frame id", async () => {
  const ids: (string | undefined)[] = [];
  await readSseEvents(chunked(['id: 1\ndata: {"type":"a"}\n\nid: 2\ndata: {"type":"done"}\n\nid: 3\ndata: {"type":"after"}\n\n']), (ev: any, frame) => {
    ids.push(frame.id);
    return ev.type === "done";
  });
  assert.deepEqual(ids, ["1", "2"]);
});

test("pings count as activity: a slow stream with heartbeats does not stall", async () => {
  const seen = await collect(timed([": ping\n\n", ": ping\n\n", ": ping\n\n", 'data: {"type":"done"}\n\n'], 30), { stallMs: 120 });
  assert.deepEqual(seen.map((e) => e.type), ["done"]);
});

test("a silent connection rejects with SseStallError (connection stalled)", async () => {
  await assert.rejects(collect(timed(['data: {"type":"a"}\n\n'], 1, true), { stallMs: 60 }), (err: any) => {
    assert.ok(err instanceof SseStallError);
    assert.match(err.message, /^Connection stalled/);
    return true;
  });
});

// ---------------------------------------------------------------------------------------------------------------
// Job streams resume from the last seen seq
// ---------------------------------------------------------------------------------------------------------------

const noAuth = (extra: Record<string, string> = {}) => extra;
const sse = (body: ReadableStream<Uint8Array>) => new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });

test("streamJobEvents reconnects after a dropped connection with after=<last seq>", async () => {
  const urls: string[] = [];
  let call = 0;
  await withFetch(
    async (u) => {
      urls.push(u);
      call += 1;
      if (call === 1) {
        return sse(chunked(['id: 4\ndata: {"seq":4,"type":"thought","data":{"id":"a"}}\n\n', 'id: 5\ndata: {"seq":5,"type":"content_delta","delta":"x"}\n\n'], new TypeError("network error")));
      }
      return sse(chunked(['id: 6\ndata: {"seq":6,"type":"done","result":{"response":"ok"}}\n\n']));
    },
    async () => {
      const types: string[] = [];
      await streamJobEvents("http://x", noAuth, "job_1", 3, (ev: any) => {
        types.push(ev.type);
        return ev.type === "done";
      }, undefined, { reconnectDelayMs: 1 });
      assert.deepEqual(types, ["thought", "content_delta", "done"]);
    }
  );
  assert.deepEqual(urls, ["http://x/api/jobs/job_1/events?after=3", "http://x/api/jobs/job_1/events?after=5"]);
});

test("streamJobEvents returns on a clean end with no new events, and does not retry HTTP errors", async () => {
  let calls = 0;
  await withFetch(
    async () => {
      calls += 1;
      return sse(chunked([": ping\n\n"]));
    },
    async () => {
      await streamJobEvents("http://x", noAuth, "job_1", 0, () => false, undefined, { reconnectDelayMs: 1 });
    }
  );
  assert.equal(calls, 1);

  calls = 0;
  await withFetch(
    async () => {
      calls += 1;
      return jsonResponse(404, { detail: "Job not found" });
    },
    async () => {
      await assert.rejects(streamJobEvents("http://x", noAuth, "job_1", 0, () => false, undefined, { reconnectDelayMs: 1 }), (err: any) => {
        assert.equal(err.status, 404);
        assert.equal(err.message, "Job not found");
        return true;
      });
    }
  );
  assert.equal(calls, 1);
});

test("streamJobEvents gives up after maxReconnects failures without progress", async () => {
  let calls = 0;
  await withFetch(
    async () => {
      calls += 1;
      throw new TypeError("Failed to fetch");
    },
    async () => {
      await assert.rejects(
        streamJobEvents("http://x", noAuth, "job_1", 0, () => false, undefined, { reconnectDelayMs: 1, maxReconnects: 2 }),
        (err: any) => err instanceof ApiError && err.status === 0
      );
    }
  );
  assert.equal(calls, 3);
});

test("streamJobEvents stops reconnecting when aborted", async () => {
  const controller = new AbortController();
  let calls = 0;
  await withFetch(
    async () => {
      calls += 1;
      setTimeout(() => controller.abort(), 0);
      throw new TypeError("Failed to fetch");
    },
    async () => {
      await assert.rejects(streamJobEvents("http://x", noAuth, "job_1", 0, () => false, controller.signal, { reconnectDelayMs: 200 }));
    }
  );
  assert.equal(calls, 1);
});
