# Clarp relay

A Cloudflare Worker with one Durable Object per Host. It lets the Clarp apps reach
your own Host from anywhere, without a VPN, a public address or a domain of your own.

The Host dials out to the relay with one WebSocket (`/connect`) and keeps it open.
An app uses `https://<your relay>/h/<host id>` as the Host's address; every request
it makes there (plain HTTP, event streams, audio, the terminal and other WebSockets)
travels down that socket to the Host and back. The relay checks only that the Host
is who it says it is. The apps are checked by the Host itself, with the device token
each one got when it was paired, exactly as on any other route.

## Deploy your own relay

You need a Cloudflare account (the free plan is enough: Durable Objects with SQLite
storage are included) and Node.js with npm.

```bash
cd relay
npm ci
npx wrangler login
```

Pick a name of your own for the Worker in `wrangler.toml` (`name = "..."`). The relay
will be served at `https://<name>.<your-account-subdomain>.workers.dev`.

Set the relay's secret. It never leaves Cloudflare and no Host or app ever needs it;
make it long and random:

```bash
openssl rand -hex 32 | npx wrangler secret put RELAY_SECRET
```

Keep a copy somewhere private (a password manager): you need it to make a key for
each Host. Then deploy:

```bash
npx wrangler deploy
curl https://<name>.<your-account-subdomain>.workers.dev/   # answers "clarp relay"
```

## Point a Host at it

Each Host has a name at the relay (its host id: 8 to 64 lowercase letters, digits and
hyphens) and a key, `HMAC-SHA256(RELAY_SECRET, host_id)` in hex. Use a random name: it
is part of the address the apps are paired with, so anyone who knows it can knock on
your Host's door (the Host still refuses everyone without a device token).

```bash
HOST_ID="clarp-$(openssl rand -hex 8)"
read -rs RELAY_SECRET          # paste the secret; it is not echoed
KEY="$(printf %s "$HOST_ID" | openssl dgst -sha256 -hmac "$RELAY_SECRET" -r | cut -d' ' -f1)"
unset RELAY_SECRET
```

On the Host's machine, write the relay's address, the name and the key to
`~/.config/clarp-host/relay.json`, readable by its owner only (the Host refuses a file
others can read):

```bash
umask 077
mkdir -p ~/.config/clarp-host
printf '{"url": "https://%s", "host_id": "%s", "key": "%s"}\n' \
  "<name>.<your-account-subdomain>.workers.dev" "$HOST_ID" "$KEY" > ~/.config/clarp-host/relay.json
unset KEY
```

Then turn the relay on and restart the Host with its installer, either one you
built or the published Linux build (which passes its options on to `install-host`):

```bash
server-rs/tools/install-host --relay on --start
server-rs/tools/install-from-release --relay on
```

This writes `[network] relay_enabled = true` into the Host's own config. (A Host
started by hand takes the same switch with `clarp-host --relay-config FILE`,
where FILE has `[network] relay_enabled = true` and `relay.json` beside it.) Many
agents need many open files, about three for each agent: the Host raises its own soft
open-files limit to the hard limit at start (at most 65,536), so in a container give it
a high hard limit (`docker run --ulimit nofile=65536:65536`). `install-host` copies a
numeric `LimitNOFILE` from the legacy unit and uses 65,536 as its fallback. The Host
dials the relay only when it has an administrator token
(`CLARP_HOST_TOKEN`): a Host anyone could speak to is never put on the relay.

Check the connection, then pair an app through it:

```bash
clarp-host network relay status
CLARP_HOST_TOKEN="$(sed -n 's/^CLARP_HOST_TOKEN=//p' ~/.config/clarp-host/env)" \
  clarp-admin pair create --relay --name "My phone"
```

Pairing answers only the Host's owner, so it is run with the Host's administrator token,
which the installer keeps in `~/.config/clarp-host/env`.

`pair create --relay` prints a one-use QR code and `clarp://` link that carries the
relay address `https://<relay>/h/<host id>`. Scan it with the app.

Only one Host may use a name. The relay gives a name's apps to whichever Host dialled
last, so never give two Hosts the same `relay.json`.

## What the relay can see, and what the secret protects

- **TLS ends at the Worker.** The app's connection is HTTPS to Cloudflare, and the
  Host's socket is a WSS connection to Cloudflare. Inside the Worker everything is
  readable: request paths, headers (including each app's device token) and bodies, and
  everything the Host answers. This is not end-to-end encryption. Whoever controls the
  Cloudflare account the relay runs in, and Cloudflare itself, are trusted with that.
  That is why you deploy your own.
- **The relay keeps nothing.** The Durable Object stores no data. It holds the Host's
  socket and the streams in flight in memory only, and forgets them when they close.
- **The secret protects a Host's name.** Only someone who holds the key for a host id
  can connect as that Host, and the key can only be made with `RELAY_SECRET`. Someone
  who has a Host's key can take its place at the relay and receive the requests its
  apps send, device tokens included. Keep the secret out of every Host, and keep
  `relay.json` at mode 600. If a key leaks, give the Host a new host id (and re-pair its
  apps); if the secret leaks, set a new one and make new keys for every Host.
- **Logs.** Cloudflare's request logs (Workers Logs, `wrangler tail`, Logpush) record
  the full URL of each request. The relay takes a Host's key in an
  `Authorization: Bearer` header, which is not in the URL. It still accepts the key as a
  `?key=` query parameter for connectors that send it there, and the Clarp Host
  currently does, so the key appears in your Worker's request logs. Anyone with access
  to those logs can read it. Moving the Host to the header is
  [issue #876](https://github.com/Maxteabag/clarp/issues/876).
- **Strangers.** Anyone who knows a host id can send requests to it. The relay forwards
  them to the Host, which refuses them without a device token and answers 429 after
  repeated failures from one address. While the Host is offline the relay answers 503
  for every host id, whether one ever existed or not. The relay lets one Host have at
  most 256 open streams (requests and WebSockets together) and answers 429 above that.

## Develop

```bash
npm ci
npm test            # runs in the Workers runtime locally, with a test-only secret
npm run typecheck
npx wrangler dev    # needs RELAY_SECRET in .dev.vars (kept out of git)
```

The tests need no Cloudflare account and deploy nothing.
