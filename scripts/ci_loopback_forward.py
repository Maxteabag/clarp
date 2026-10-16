#!/usr/bin/env python3
"""Loopback TCP forwarder for CI test containers on an internal Docker network.

    ci_loopback_forward.py TARGET_IP TARGET_PORT PORT_FILE

Listens on 127.0.0.1 (an ephemeral port, written to PORT_FILE) and forwards every connection to
TARGET_IP:TARGET_PORT. Test clients keep a loopback origin, which is a browser secure context and
satisfies the pairing helper's loopback-only rule, while no Docker port is published.
"""
import asyncio
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def main(target, port, port_file):
    async def handle(client_reader, client_writer):
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(target, port)
        except OSError as error:
            print(f"forward to {target}:{port} failed: {error}", file=sys.stderr, flush=True)
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer),
                             return_exceptions=True)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    with open(port_file, "w") as handle_file:
        handle_file.write(str(server.sockets[0].getsockname()[1]))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3]))
