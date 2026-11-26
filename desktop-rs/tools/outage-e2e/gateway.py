#!/usr/bin/env python3
"""A gateway in front of a disposable Clarp Host, as a reverse proxy in front of a real one
behaves: it forwards each connection; when the Host does not accept it, or when the mode
file says 502, it answers `502 Bad Gateway`. Switching to 502 cuts every open connection
(the event stream included). One JSON line per connection in the log."""
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

async def handle(reader, writer):
    task = asyncio.current_task()
    active.add(task)
    try:
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = await reader.read(4096)
            if not chunk:
                return
            head += chunk
        lines = head.split(b"\r\n")
        request = lines[0].decode("latin1")
        last = next((l.split(b":", 1)[1].strip().decode("latin1") for l in lines[1:] if l.lower().startswith(b"last-event-id:")), "")
        if mode() == "502":
            log(request=request, last_event_id=last, outcome="502 (gateway mode)")
            writer.write(BAD); await writer.drain(); writer.close(); return
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", upstream_port)
        except OSError as error:
            log(request=request, last_event_id=last, outcome=f"502 (Host refused: {error.strerror})")
            writer.write(BAD); await writer.drain(); writer.close(); return
        log(request=request, last_event_id=last, outcome="forwarded")
        up_writer.write(head); await up_writer.drain()
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
    except asyncio.CancelledError:
        try:
            writer.transport.abort()
        except Exception:
            pass
    finally:
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
