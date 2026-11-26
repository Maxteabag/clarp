import { env, exports } from 'cloudflare:workers';
import { describe, it, expect, afterEach } from 'vitest';
import { HostRelay, MAX_STREAMS } from '../src/index';

const encoder = new TextEncoder();
function frame(kind: number, id: number, payload = new Uint8Array()) {
  const bytes = new Uint8Array(5 + payload.length);
  bytes[0] = kind;
  new DataView(bytes.buffer).setUint32(1, id);
  bytes.set(payload, 5);
  return bytes.buffer;
}
function json(kind: number, id: number, value: unknown) {
  return frame(kind, id, encoder.encode(JSON.stringify(value)));
}
function unpack(data: ArrayBuffer | Uint8Array) {
  const bytes = new Uint8Array(data);
  return { kind: bytes[0], id: new DataView(bytes.buffer).getUint32(1), payload: bytes.slice(5) };
}
const sockets: WebSocket[] = [];
afterEach(() => { for (const ws of sockets.splice(0)) { try { ws.close(); } catch {} } });

// Real Workers streams, Response and WebSocket APIs with a minimal DO event context.
function relay(tags = new Map<WebSocket, string[]>()) {
  const sent: ArrayBuffer[] = [];
  const host = { send: (data: ArrayBuffer) => sent.push(data), readyState: 1 } as unknown as WebSocket;
  tags.set(host, ['host']);
  const context = {
    setWebSocketAutoResponse() {},
    getWebSockets: (tag?: string) => [...tags].filter(([, t]) => !tag || t.includes(tag)).map(([ws]) => ws),
    getTags: (ws: WebSocket) => tags.get(ws) ?? [],
    acceptWebSocket(ws: WebSocket, value: string[]) { ws.accept(); tags.set(ws, value); sockets.push(ws); },
  } as unknown as DurableObjectState;
  return { object: new HostRelay(context, env as any), host, sent, tags };
}

async function signed(hostID: string) {
  const key = await crypto.subtle.importKey('raw', encoder.encode('test-only-relay-secret'), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const signature = new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(hostID)));
  return [...signature].map(b => b.toString(16).padStart(2, '0')).join('');
}

describe('public routing and authentication', () => {
  it('reports health and an offline host', async () => {
    expect(await (await exports.default.fetch('https://relay.test/')).text()).toBe('clarp relay\n');
    expect((await exports.default.fetch('https://relay.test/h/offline-test/status')).status).toBe(503);
  });
  it('accepts a signed connector and relays the public prefix', async () => {
    const hostID = 'signed-test';
    const key = await crypto.subtle.importKey('raw', encoder.encode('test-only-relay-secret'), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    const signature = new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(hostID)));
    const hex = [...signature].map(b => b.toString(16).padStart(2, '0')).join('');
    const response = await exports.default.fetch(`https://relay.test/connect?host=${hostID}&key=${hex}`, { headers: { Upgrade: 'websocket' } });
    expect(response.status).toBe(101);
    const connector = response.webSocket!; connector.binaryType = 'arraybuffer'; connector.accept(); sockets.push(connector);
    const received = new Promise<any>(resolve => connector.addEventListener('message', event => {
      const f = unpack(event.data);
      if (f.kind === 1) {
        resolve(JSON.parse(new TextDecoder().decode(f.payload)));
        connector.send(json(0x11, f.id, { status: 200, headers: { 'content-type': 'application/json' } }));
        connector.send(frame(0x12, f.id, encoder.encode('{"ready":true}')));
        connector.send(frame(0x13, f.id));
      }
    }));
    const result = await exports.default.fetch(`https://relay.test/h/${hostID}/status?q=one`, { headers: { Authorization: 'Bearer test-device', 'X-Relay-Path': '/spoof' } });
    expect(await result.json()).toEqual({ ready: true });
    const request = await received;
    expect(request.path).toBe('/status?q=one');
    expect(request.passthrough_encoding).toBe(true);
    expect(request.headers['x-forwarded-prefix']).toBe(`/h/${hostID}`);
    expect(request.headers.authorization).toBe('Bearer test-device');
  });
  it('rejects invalid connector credentials', async () => {
    expect((await exports.default.fetch('https://relay.test/connect?host=host-test&key=wrong', { headers: { Upgrade: 'websocket' } })).status).toBe(403);
    expect((await exports.default.fetch('https://relay.test/connect?host=host-test', { headers: { Upgrade: 'websocket', Authorization: 'Bearer wrong' } })).status).toBe(403);
    expect((await exports.default.fetch('https://relay.test/connect?host=host-test', { headers: { Upgrade: 'websocket' } })).status).toBe(403);
  });
  it('accepts the connector key in an Authorization header, out of the address', async () => {
    const hostID = 'header-test';
    const response = await exports.default.fetch(`https://relay.test/connect?host=${hostID}`, { headers: { Upgrade: 'websocket', Authorization: `Bearer ${await signed(hostID)}` } });
    expect(response.status).toBe(101);
    response.webSocket!.accept(); sockets.push(response.webSocket!);
  });
  it('does not take one host key for another host id', async () => {
    const response = await exports.default.fetch('https://relay.test/connect?host=other-test', { headers: { Upgrade: 'websocket', Authorization: `Bearer ${await signed('header-test')}` } });
    expect(response.status).toBe(403);
  });
  it('does not let public headers register an unauthenticated connector', async () => {
    const response = await exports.default.fetch('https://relay.test/h/spoof-test/status', {
      headers: { Upgrade: 'websocket', 'X-Relay-Connect': '1' },
    });
    if (response.webSocket) { response.webSocket.accept(); sockets.push(response.webSocket); }
    expect(response.status).toBe(503);
  });
});

describe('HTTP frame lifecycle', () => {
  it('frames request bodies and preserves response bytes and headers', async () => {
    const { object, host, sent } = relay();
    const response = object.fetch(new Request('https://relay.test/agents/snapshot?q=one', { method: 'POST', body: 'hello', headers: { 'x-relay-client-encoding': 'gzip;q=0', 'accept-encoding': 'br, gzip' } }));
    await new Promise(resolve => setTimeout(resolve, 0));
    const frames = sent.map(unpack);
    expect(frames.map(f => f.kind)).toEqual([1, 2, 3]);
    const metadata = JSON.parse(new TextDecoder().decode(frames[0].payload));
    expect(metadata.path).toBe('/agents/snapshot?q=one');
    expect(metadata.passthrough_encoding).toBe(true);
    expect(metadata.headers['accept-encoding']).toBe('gzip;q=0');
    expect(metadata.headers['x-relay-client-encoding']).toBeUndefined();
    expect(new TextDecoder().decode(frames[1].payload)).toBe('hello');
    await object.webSocketMessage(host, json(0x11, 1, { status: 200, headers: { 'content-type': 'application/json', 'transfer-encoding': 'chunked' } }));
    const res = await response;
    const body = res.text();
    await object.webSocketMessage(host, frame(0x12, 1, encoder.encode('{"ok":true}')));
    await object.webSocketMessage(host, frame(0x13, 1));
    expect(await body).toBe('{"ok":true}');
    expect(res.headers.get('transfer-encoding')).toBeNull();
  });
  it('preserves precompressed snapshot bytes and their encoding header', async () => {
    const { object, host } = relay();
    const bytes = new Uint8Array(await new Response(new Blob(['{"agents":[]}']).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer());
    const pending = object.fetch(new Request('https://relay.test/agents/snapshot'));
    await object.webSocketMessage(host, json(0x11, 1, { status: 200, headers: { 'content-encoding': 'gzip', 'content-type': 'application/json' } }));
    const response = await pending;
    const body = response.arrayBuffer();
    await object.webSocketMessage(host, frame(0x12, 1, bytes));
    await object.webSocketMessage(host, frame(0x13, 1));
    expect(response.headers.get('content-encoding')).toBe('gzip');
    expect(new Uint8Array(await body)).toEqual(bytes);
  });
  it('aborts upstream when the reader cancels an SSE stream', async () => {
    const { object, host, sent } = relay();
    const response = object.fetch(new Request('https://relay.test/events'));
    await object.webSocketMessage(host, json(0x11, 1, { status: 200, headers: { 'content-type': 'text/event-stream' } }));
    await (await response).body!.cancel();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(sent.map(unpack).some(f => f.kind === 0x7f && f.id === 1)).toBe(true);
  });
  it('bounds stalled streams without blocking other responses', async () => {
    const { object, host, sent } = relay();
    const response = object.fetch(new Request('https://relay.test/events'));
    await object.webSocketMessage(host, json(0x11, 1, { status: 200, headers: {} }));
    await response;
    for (let i = 0; i < 65; i++) await object.webSocketMessage(host, frame(0x12, 1, encoder.encode('ping')));
    expect(sent.map(unpack).some(f => f.kind === 0x7f && f.id === 1)).toBe(true);
    const second = object.fetch(new Request('https://relay.test/status'));
    await object.webSocketMessage(host, json(0x11, 2, { status: 204, headers: {} }));
    await object.webSocketMessage(host, frame(0x13, 2));
    expect((await second).status).toBe(204);
  });
  it('does not abort a replacement connector when the old socket closes', async () => {
    const { object, host, tags } = relay();
    const old = {} as WebSocket; tags.set(old, ['host']);
    const pending = object.fetch(new Request('https://relay.test/status'));
    await object.webSocketClose(old, 1000, 'replaced');
    await object.webSocketMessage(host, json(0x11, 1, { status: 204, headers: {} }));
    await object.webSocketMessage(host, frame(0x13, 1));
    expect((await pending).status).toBe(204);
  });
  it('answers 429 past the stream limit and takes streams again once one ends', async () => {
    const { object, host, sent } = relay();
    const open = [];
    for (let i = 0; i < MAX_STREAMS; i++) open.push(object.fetch(new Request('https://relay.test/events')));
    const refused = await object.fetch(new Request('https://relay.test/status'));
    expect(refused.status).toBe(429);
    expect(refused.headers.get('retry-after')).toBe('5');
    await object.webSocketMessage(host, json(0x11, 1, { status: 204, headers: {} }));
    await object.webSocketMessage(host, frame(0x13, 1));
    expect((await open[0]).status).toBe(204);
    const before = sent.length;
    object.fetch(new Request('https://relay.test/status'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(sent.slice(before).map(unpack)[0].kind).toBe(1);
  });
  it('returns a gateway failure when the host aborts before headers', async () => {
    const { object, host } = relay();
    const pending = object.fetch(new Request('https://relay.test/status'));
    await object.webSocketMessage(host, frame(0x7f, 1));
    expect((await pending).status).toBe(502);
  });
  it('aborts orphan body frames but does not bounce terminal frames', async () => {
    const { object, host, sent } = relay();
    await object.webSocketMessage(host, frame(0x12, 123));
    await object.webSocketMessage(host, frame(0x13, 123));
    await object.webSocketMessage(host, frame(0x7f, 123));
    expect(sent.map(unpack).map(f => [f.kind, f.id])).toEqual([[0x7f, 123]]);
  });
});

describe('phone WebSocket tunnel', () => {
  it('preserves text/binary frames and maps unsupported close codes', async () => {
    const { object, host, sent, tags } = relay();
    const response = await object.fetch(new Request('https://relay.test/terminal/session', { headers: { Upgrade: 'websocket' } }));
    const phone = response.webSocket!; phone.accept(); sockets.push(phone);
    expect(unpack(sent[0]).kind).toBe(0x21);
    const server = [...tags.keys()].find(ws => ws !== host)!;
    await object.webSocketMessage(server, 'hello');
    expect([...unpack(sent[1]).payload]).toEqual([1, ...encoder.encode('hello')]);
    await object.webSocketMessage(server, new Uint8Array([0, 255]).buffer);
    expect([...unpack(sent[2]).payload]).toEqual([0, 0, 255]);
    const message = new Promise<MessageEvent>(resolve => phone.addEventListener('message', resolve, { once: true }));
    await object.webSocketMessage(host, frame(0x23, 1, new Uint8Array([1, ...encoder.encode('answer')])));
    expect((await message).data).toBe('answer');
    const closed = new Promise<CloseEvent>(resolve => phone.addEventListener('close', resolve, { once: true }));
    await object.webSocketMessage(host, json(0x24, 1, { code: 1012, reason: 'restart' }));
    expect((await closed).code).toBe(4012);
  });
});

describe('hibernation', () => {
  it('finds a tunneled phone socket again and does not reuse its stream id', async () => {
    const pair = new WebSocketPair();
    const [phone, server] = [pair[0], pair[1]];
    phone.accept(); server.accept(); sockets.push(phone, server);
    // A fresh object (memory gone) with a tunnel that stayed open as stream 1.
    const { object, host, sent } = relay(new Map([[server, ['client', '1']]]));
    const message = new Promise<MessageEvent>(resolve => phone.addEventListener('message', resolve, { once: true }));
    await object.webSocketMessage(host, frame(0x23, 1, new Uint8Array([1, ...encoder.encode('still here')])));
    expect((await message).data).toBe('still here');
    expect(sent.map(unpack).some(f => f.kind === 0x7f)).toBe(false);
    object.fetch(new Request('https://relay.test/status'));
    await new Promise(resolve => setTimeout(resolve, 0));
    const request = sent.map(unpack).find(f => f.kind === 1)!;
    expect(request.id).not.toBe(1);
  });
});
