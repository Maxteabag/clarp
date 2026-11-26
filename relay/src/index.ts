/**
 * Clarp relay: a Worker + one Durable Object per Host.
 *
 * The Host dials OUT with a WebSocket to /connect and keeps it open.
 * Phones talk plain HTTPS to /h/<host_id>/<clarp path>. The Durable Object
 * turns each phone request into frames on the Host socket, the connector on
 * the Host performs it against Clarp on loopback and streams the answer back.
 * SSE and chunked audio are plain streamed responses; phone WebSockets
 * (terminal, Oracle realtime) are tunneled as a second stream kind.
 *
 * Frame layout (binary): [kind u8][stream id u32 BE][payload]
 *
 * The Host proves its name with key = HMAC-SHA256(RELAY_SECRET, host_id), sent as
 * "Authorization: Bearer <key>". A key in the query string (?key=) is still taken from
 * connectors that send it there, but an address is written into request logs, so a
 * header is the way to send it.
 */
import { MAX_STREAMS } from "./limits";
export interface Env {
  HOST_RELAY: DurableObjectNamespace;
  RELAY_SECRET: string;
}

const K = {
  REQ: 0x01, REQ_BODY: 0x02, REQ_END: 0x03,
  RES_HEAD: 0x11, RES_BODY: 0x12, RES_END: 0x13,
  WS_OPEN: 0x21, WS_ACCEPT: 0x22, WS_MSG: 0x23, WS_CLOSE: 0x24,
  ABORT: 0x7f,
} as const;

const CHUNK = 256 * 1024; // stay well under the 1 MiB Durable Object message limit
const HOP_BY_HOP = new Set(["connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection", "host", "content-length"]);

function frame(kind: number, id: number, payload?: Uint8Array): Uint8Array {
  const out = new Uint8Array(5 + (payload?.length ?? 0));
  out[0] = kind;
  new DataView(out.buffer).setUint32(1, id, false);
  if (payload) out.set(payload, 5);
  return out;
}
function json(kind: number, id: number, obj: unknown): Uint8Array {
  return frame(kind, id, new TextEncoder().encode(JSON.stringify(obj)));
}
function parse(buf: ArrayBuffer): { kind: number; id: number; payload: Uint8Array } {
  const u = new Uint8Array(buf);
  return { kind: u[0], id: new DataView(buf).getUint32(1, false), payload: u.subarray(5) };
}

async function hostKey(secret: string, hostId: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(hostId));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function connectorKey(request: Request, url: URL): string {
  const auth = request.headers.get("authorization") ?? "";
  const m = auth.match(/^bearer\s+(\S+)\s*$/i);
  return m ? m[1] : (url.searchParams.get("key") ?? "");
}
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

// Script-initiated close() only accepts 1000 or 3000-4999; map server-style codes into the private range.
function safeClose(ws: WebSocket | undefined, code: number, reason: string): void {
  if (!ws) return;
  const c = code === 1000 || (code >= 3000 && code <= 4999) ? code : 4000 + (code % 1000);
  try { ws.close(c, reason.slice(0, 120)); }
  catch (e) { console.log(`close(${c}) failed: ${String(e)}`); try { ws.close(); } catch {} }
}

// ---------------------------------------------------------------- Worker ----

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/") return new Response("clarp relay\n", { headers: { "content-type": "text/plain" } });

    if (url.pathname === "/connect") {
      const hostId = url.searchParams.get("host") ?? "";
      const key = connectorKey(request, url);
      if (!/^[a-z0-9-]{8,64}$/.test(hostId)) return new Response("bad host id", { status: 400 });
      if (!timingSafeEqual(key, await hostKey(env.RELAY_SECRET, hostId))) return new Response("bad key", { status: 403 });
      if (request.headers.get("upgrade")?.toLowerCase() !== "websocket") return new Response("expected websocket", { status: 426 });
      const h = new Headers(request.headers); h.set("x-relay-connect", "1"); h.delete("authorization");
      return env.HOST_RELAY.get(env.HOST_RELAY.idFromName(hostId)).fetch(new Request(request, { headers: h }));
    }

    const m = url.pathname.match(/^\/h\/([a-z0-9-]{8,64})(\/.*)?$/);
    if (!m) return new Response("not found", { status: 404 });
    const hostId = m[1];
    const inner = new URL(request.url);
    inner.pathname = m[2] || "/";
    const headers = new Headers(request.headers);
    // Only the authenticated /connect branch may mark a connector handshake.
    // Public clients can otherwise impersonate a Host via this internal header.
    headers.delete("x-relay-connect");
    headers.set("x-relay-path", inner.pathname + inner.search);
    headers.set("x-forwarded-for", request.headers.get("cf-connecting-ip") ?? "");
    headers.set("x-forwarded-proto", "https");
    headers.set("x-forwarded-host", url.host);
    headers.set("x-forwarded-prefix", `/h/${hostId}`);
    // The edge may normalize Accept-Encoding. Preserve what this client actually offered
    // across the internal fetch; overwrite any client-supplied copy of the relay header.
    const clientEncoding = request.cf ? request.cf.clientAcceptEncoding : request.headers.get("accept-encoding");
    headers.set("x-relay-client-encoding", typeof clientEncoding === "string" ? clientEncoding : "identity");
    const forwarded = new Request(inner.toString(), { method: request.method, headers, body: request.body, redirect: "manual" });
    return env.HOST_RELAY.get(env.HOST_RELAY.idFromName(hostId)).fetch(forwarded);
  },
} satisfies ExportedHandler<Env>;

// -------------------------------------------------------- Durable Object ----

interface Pending {
  head?: (r: Response) => void;
  fail: (e: Error) => void;
  writer?: WritableStreamDefaultWriter<Uint8Array>;
  clientWs?: WebSocket;
  isWs: boolean;
  backlog?: number;
}

export class HostRelay implements DurableObject {
  private pending = new Map<number, Pending>();
  private nextId = 1;

  constructor(private ctx: DurableObjectState, private env: Env) {
    // Keep-alive without waking the object: the connector sends "ping" text frames.
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  // A tunneled phone WebSocket outlives this object's memory when it hibernates: its socket
  // is found again by its stream id, which is one of its tags.
  private client(id: number): WebSocket | undefined {
    return this.pending.get(id)?.clientWs ?? this.ctx.getWebSockets(String(id)).find((s) => this.ctx.getTags(s).includes("client"));
  }

  private streams(): number {
    let n = this.ctx.getWebSockets("client").length;
    for (const p of this.pending.values()) if (!p.isWs) n++;
    return n;
  }

  // A stream id not in use: after hibernation the counter starts again while tunnels stay open.
  private allocate(): number {
    for (;;) {
      const id = this.nextId++;
      if (this.nextId > 0xfffffffe) this.nextId = 1;
      if (!this.pending.has(id) && !this.client(id)) return id;
    }
  }

  private host(): WebSocket | undefined {
    const socks = this.ctx.getWebSockets("host");
    return socks.find((s) => s.readyState === WebSocket.READY_STATE_OPEN) ?? socks[0];
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (request.headers.get("x-relay-connect") === "1") return this.acceptHost(request);

    const host = this.host();
    if (!host) return new Response(JSON.stringify({ error: "host offline" }), { status: 503, headers: { "content-type": "application/json", "retry-after": "5" } });

    if (this.streams() >= MAX_STREAMS) return new Response(JSON.stringify({ error: "too many streams" }), { status: 429, headers: { "content-type": "application/json", "retry-after": "5" } });
    const id = this.allocate();
    const path = request.headers.get("x-relay-path") ?? url.pathname + url.search;
    const headers: Record<string, string> = {};
    request.headers.forEach((v, k) => { if (!HOP_BY_HOP.has(k) && !k.startsWith("cf-") && k !== "x-relay-path" && k !== "x-relay-client-encoding") headers[k] = v; });

    if (request.headers.get("upgrade")?.toLowerCase() === "websocket") return this.tunnelWebSocket(host, id, path, headers);

    const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
    const writer = writable.getWriter();
    const headPromise = new Promise<Response>((resolve, reject) => {
      this.pending.set(id, { head: resolve, fail: reject, writer, isWs: false, backlog: 0 });
    });
    // The phone closing the response (SSE reconnect, app backgrounded) errors the writable side.
    writer.closed.catch(() => { if (this.pending.delete(id)) { try { host.send(frame(K.ABORT, id)); } catch {} } });

    // The relay hands a packed answer to the phone byte for byte (below), so the Host may pack it
    // for a phone that takes gzip: a JSON answer, and an event stream that asks with gzip=1. The
    // edge compresses JSON by itself, but never an event stream. The Host negotiates with what the
    // client itself offered (the edge may normalize Accept-Encoding), so a client that refuses gzip
    // never gets it.
    headers["accept-encoding"] = request.headers.get("x-relay-client-encoding") || "identity";
    host.send(json(K.REQ, id, { method: request.method, path, headers, passthrough_encoding: true }));
    // Stream the request body in bounded chunks, then mark its end.
    if (request.body) {
      const reader = request.body.getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (let o = 0; o < value.length; o += CHUNK) host.send(frame(K.REQ_BODY, id, value.subarray(o, o + CHUNK)));
      }
    }
    host.send(frame(K.REQ_END, id));

    const timeout = setTimeout(() => this.pending.get(id)?.fail(new Error("host did not answer in 60s")), 60_000);
    try {
      const res = await headPromise;
      clearTimeout(timeout);
      // If the Host ever answers pre-compressed, hand the bytes through untouched instead of letting the
      // runtime encode them a second time (the default for a Response that carries Content-Encoding).
      const init: ResponseInit & { encodeBody?: "automatic" | "manual" } = { status: res.status, headers: res.headers };
      if (res.headers.has("content-encoding")) init.encodeBody = "manual";
      return new Response(request.method === "HEAD" || res.status === 204 || res.status === 304 ? null : readable, init);
    } catch (e) {
      clearTimeout(timeout);
      this.pending.delete(id);
      host.send(frame(K.ABORT, id));
      return new Response(JSON.stringify({ error: String((e as Error).message) }), { status: 502, headers: { "content-type": "application/json" } });
    }
  }

  private acceptHost(request: Request): Response {
    // One connector per Host: replace a stale socket rather than fanning out.
    for (const old of this.ctx.getWebSockets("host")) safeClose(old, 1012, "replaced");
    const pair = new WebSocketPair();
    this.ctx.acceptWebSocket(pair[1], ["host"]);
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  private tunnelWebSocket(host: WebSocket, id: number, path: string, headers: Record<string, string>): Response {
    const pair = new WebSocketPair();
    const client = pair[1];
    this.ctx.acceptWebSocket(client, ["client", String(id)]);
    this.pending.set(id, { fail: () => safeClose(client, 1011, "host failed"), clientWs: client, isWs: true });
    host.send(json(K.WS_OPEN, id, { path, headers }));
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  // Messages from the Host connector, or from a tunneled phone WebSocket.
  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    const tags = this.ctx.getTags(ws);
    if (tags.includes("client")) {
      const id = Number(tags[1]);
      const host = this.host();
      if (!host) { safeClose(ws, 1011, "host offline"); return; }
      const data = typeof message === "string" ? new TextEncoder().encode(message) : new Uint8Array(message);
      const payload = new Uint8Array(1 + data.length);
      payload[0] = typeof message === "string" ? 1 : 0;
      payload.set(data, 1);
      host.send(frame(K.WS_MSG, id, payload));
      return;
    }
    if (typeof message === "string") return; // control text from host (only "ping", auto-answered)
    const { kind, id, payload } = parse(message);
    let p = this.pending.get(id);
    if (!p) {
      const clientWs = this.client(id);
      if (clientWs) p = { fail: () => safeClose(clientWs, 1011, "host failed"), clientWs, isWs: true };
    }
    if (!p) { if (kind !== K.ABORT && kind !== K.RES_END && kind !== K.WS_CLOSE) ws.send(frame(K.ABORT, id)); return; }
    switch (kind) {
      case K.RES_HEAD: {
        const h = JSON.parse(new TextDecoder().decode(payload)) as { status: number; headers: Record<string, string> };
        const headers = new Headers();
        for (const [k, v] of Object.entries(h.headers)) if (!HOP_BY_HOP.has(k.toLowerCase())) headers.append(k, v);
        p.head?.(new Response(null, { status: h.status, headers }));
        p.head = undefined;
        break;
      }
      case K.RES_BODY: {
        if (!p.writer) break;
        // Do not await: a stalled reader must not block frames for other streams.
        p.backlog = (p.backlog ?? 0) + 1;
        p.writer.write(payload.slice()).then(() => { p.backlog = (p.backlog ?? 1) - 1; }, () => {});
        if (p.backlog > 64) { // ~16 MB unread: the reader is gone or hopeless
          this.pending.delete(id); ws.send(frame(K.ABORT, id));
          p.writer.abort("reader stalled").catch(() => {});
        }
        break;
      }
      case K.RES_END: p.writer?.close().catch(() => {}); this.pending.delete(id); break;
      case K.WS_ACCEPT: break;
      case K.WS_MSG: {
        if (payload[0] === 1) p.clientWs?.send(new TextDecoder().decode(payload.subarray(1)));
        else p.clientWs?.send(payload.slice(1));
        break;
      }
      case K.WS_CLOSE: {
        const c = JSON.parse(new TextDecoder().decode(payload)) as { code?: number; reason?: string };
        safeClose(p.clientWs, c.code ?? 1000, c.reason ?? "");
        this.pending.delete(id);
        break;
      }
      case K.ABORT: p.fail(new Error("aborted by host")); await p.writer?.abort("host aborted").catch(() => {}); this.pending.delete(id); break;
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string): Promise<void> {
    const tags = this.ctx.getTags(ws);
    if (tags.includes("client")) {
      const id = Number(tags[1]);
      this.host()?.send(json(K.WS_CLOSE, id, { code, reason }));
      this.pending.delete(id);
      return;
    }
    // A replaced connector may finish its close handshake after its successor
    // has already started serving requests. Those streams belong to the new one.
    const activeHost = this.host();
    if (activeHost && activeHost !== ws) return;
    // Host went away: fail everything in flight so phones reconnect.
    for (const [id, p] of this.pending) {
      p.fail(new Error("host disconnected"));
      await p.writer?.abort("host disconnected").catch(() => {});
      safeClose(p.clientWs, 1012, "host disconnected");
      this.pending.delete(id);
    }
    for (const c of this.ctx.getWebSockets("client")) safeClose(c, 1012, "host disconnected");
  }

  async webSocketError(ws: WebSocket, _e: unknown): Promise<void> {
    await this.webSocketClose(ws, 1011, "error");
  }
}
