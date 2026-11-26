#!/usr/bin/env python3
"""A gateway in front of a disposable Clarp Host, as a reverse proxy in front of a real one
behaves: it forwards each connection; when the Host does not accept it, or when the mode
file says so, it answers `502 Bad Gateway`. Modes: `pass`; `502` (every request; switching to
it cuts every open connection, the event stream included); `reads` (only `/log`,
`/agents/snapshot` and `/live`, judged per request on kept-alive connections too, so the event
stream can come back while the reads a catch-up needs still fail). One JSON line per request."""
import asyncio, json, sys, time

listen_port, upstream_port, mode_file, log_file = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
active = set()
BODY = b"<html><body><h1>502 Bad Gateway</h1></body></html>"
BAD = b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/html\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(BODY), BODY)

def mode():
    try:
        return open(mode_file).read().strip() or "pass"
    except OSError:
        return "pass"

def log(**entry):
    entry["wall_ms"] = int(time.time() * 1000)
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")

async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

READS = ("/log", "/agents/snapshot", "/live")

def refused(request):
    """Whether the gateway answers this request 502 in the current mode: `502` all of them,
    `reads` the Host's reads a catch-up needs (the stream and the rest pass)."""
    now = mode()
    path = request.split(" ")[1] if " " in request else ""
    return now == "502" or (now == "reads" and path.split("?")[0].startswith(READS))

async def read_request(reader):
    """One request's head and body (by Content-Length), or None at the end of the connection."""
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await reader.read(1)
        if not chunk:
            return None
        head += chunk
        if len(head) > 65536:
            return None
    lines = head.split(b"\r\n")
    length = next((int(l.split(b":", 1)[1]) for l in lines[1:] if l.lower().startswith(b"content-length:")), 0)
    body = await reader.readexactly(length) if length else b""
    return head, body

def described(head):
    lines = head.split(b"\r\n")
    request = lines[0].decode("latin1")
    last = next((l.split(b":", 1)[1].strip().decode("latin1") for l in lines[1:] if l.lower().startswith(b"last-event-id:")), "")
    return request, last

async def handle(reader, writer):
    task = asyncio.current_task()
    active.add(task)
    up_writer = None
    try:
        first = await read_request(reader)
        if first is None:
            return
        request, last = described(first[0])
        if refused(request):
            log(request=request, last_event_id=last, outcome=f"502 ({mode()} mode)")
            writer.write(BAD); await writer.drain(); writer.close(); return
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", upstream_port)
        except OSError as error:
            log(request=request, last_event_id=last, outcome=f"502 (Host refused: {error.strerror})")
            writer.write(BAD); await writer.drain(); writer.close(); return
        log(request=request, last_event_id=last, outcome="forwarded")
        up_writer.write(first[0] + first[1]); await up_writer.drain()
        back = asyncio.create_task(pipe(up_reader, writer))
        # Every later request on a kept-alive connection is judged by itself.
        while True:
            nxt = await read_request(reader)
            if nxt is None:
                break
            request, last = described(nxt[0])
            if refused(request):
                log(request=request, last_event_id=last, outcome=f"502 ({mode()} mode, kept-alive connection)")
                writer.write(BAD); await writer.drain()
                break
            log(request=request, last_event_id=last, outcome="forwarded (kept-alive connection)")
            up_writer.write(nxt[0] + nxt[1]); await up_writer.drain()
        back.cancel()
        writer.close()
    except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError):
        try:
            writer.transport.abort()
        except Exception:
            pass
    finally:
        if up_writer is not None:
            try:
                up_writer.close()
            except Exception:
                pass
        active.discard(task)

async def watch():
    last = mode()
    while True:
        await asyncio.sleep(0.05)
        now = mode()
        if now != last:
            log(mode_change=f"{last} -> {now}", cut=len(active) if now == "502" else 0)
            if now == "502":
                for task in list(active):
                    task.cancel()
            last = now

async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", listen_port)
    log(started=f"127.0.0.1:{listen_port} -> 127.0.0.1:{upstream_port}")
    asyncio.create_task(watch())
    async with server:
        await server.serve_forever()

asyncio.run(main())
