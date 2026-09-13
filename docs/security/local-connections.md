# Automatic encrypted local connections

Enable local access on the computer once:

```bash
clarp-admin network local enable
clarp-admin network local status
```

The **Pair iPhone** screen also has **Allow encrypted local connections**.
Clarp creates a private TLS identity automatically, opens HTTPS on port 7683,
and advertises `_clarps._tcp` through Bonjour. If the default port is occupied,
setup selects and saves an available port automatically. A later local-listener
startup failure leaves the primary Host running and is reported by local status. Tailscale and the relay retain
their current settings. No router port forwarding or public DNS is needed.
For a different port use `enable --port 8763`. Disable with `network local disable`.
The Host firewall must allow the selected port and Bonjour on the local network;
guest networks may isolate devices and prevent direct connections.

Pair normally using your trusted HTTPS Tailscale or relay address. The iPhone
learns the Host's local certificate fingerprint from authenticated `/server-info`
over that HTTPS connection and remembers it. With **Prefer local connection**
enabled in the app's Computer connection settings, allow iOS Local Network access.
The app discovers and probes local addresses, verifies the remembered certificate
before sending a device token, and chooses direct HTTPS when it is reachable.
The computer may be on Ethernet while the phone uses Wi-Fi on the same LAN.

When local connectivity disappears, the app returns to the original configured
Tailscale or relay address. It never disables the VPN globally or creates a
second Computer entry. Route changes reconnect live events while keeping their
replay cursor. An operation already submitted is not automatically replayed on
another route: a failed write may have reached the Host.

The initial trusted HTTPS connection is required to learn the local identity.
After that, remembered local credentials work even if Internet access is down.
Bonjour alone does not establish identity; its announcements are untrusted.
Neither an identical Wi-Fi name nor an advertised server ID is authentication.

The LAN endpoint always requires device authentication, including requests from
loopback. The administrator credential stays on the original loopback endpoint.
API, live events, WebSockets and media retain encryption and certificate checking.
A LAN request never falls back to plaintext HTTP to bypass a certificate failure.

The key/certificate file lives in `local-tls/identity.pem` beside config.toml,
with mode 0600. Keep it in the Host's private backups. It persists across upgrades;
a missing identity generates a new one, so clients must refresh their remembered
identity through the trusted remote connection. Conflicting pins fail closed;
restart the app after a deliberate certificate replacement. Certificates are
valid for ten years; expiry fails closed and requires explicit renewal.

Local discovery currently advertises private IPv4 interfaces and `.local` names.
IPv6-only LANs and networks that block discovery/direct access use the configured
remote connection. The relay remains a trusted endpoint during initial enrollment
and remote operation; this feature does not change its existing trust model.
