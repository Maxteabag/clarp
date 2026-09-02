# Clarp onboarding contract

This document fixes the ownership boundaries shared by the CLI, Textual UI,
server, and iOS client. User interfaces call these contracts; they do not
implement a second installer.

## Voice output

Voice output is scoped to one Computer. Supported choices are:

- `cartesia`: cloud streaming, recommended when an API key is configured.
- `elevenlabs`: cloud streaming.
- `deepgram`: cloud streaming.
- `clarp`: managed Clarp Voice, routed through Audio Central instead of a
  Computer-local key.
- a custom adapter id: local or self-hosted synthesis you install yourself.
- `none`: no synthesized speech.

Fallback is an explicit second choice and defaults to `none`; Clarp never
silently moves work to another paid provider. Agent voice values remain a
provider-to-voice mapping. A missing mapping is reported rather than charged to
an unrelated provider.

## Pairing

`clarp-admin pair create` creates a random, single-use bootstrap code. Only a
SHA-256 digest is stored. The code expires after ten minutes by default and is
exchanged through the public `/pairing/exchange` endpoint for a random,
revocable device token. The QR/deep link contains the public URL, server
identity, requested access level, expiry, and bootstrap code. It never contains
the server administrator bearer token.

Device tokens are stored by iOS in Keychain. `full` devices may use every API;
`limited` devices may chat and read state but may not mutate Computer settings,
credentials, updates, skills, or pairing state. Revocation takes effect on the
next request.

## Networking

Tailscale is optional. A Computer may use:

- `tailscale`: loopback server plus HTTPS Tailscale Serve.
- `lan`: loopback/LAN bind plus Bonjour `_clarp._tcp` discovery.
- `manual`: an operator-provided HTTPS reverse-proxy URL.
- `off`: local-only access.

Changing network exposure is explicit. The setup UI may detect Tailscale but
must not modify Serve, Funnel, DNS, firewall, or public exposure without the
user selecting that mode. Public Funnel is not configured by Clarp.

## Interfaces

- `clarp-admin` subcommands are the automation and recovery authority.
- `clarp-tui` is the friendly setup/configuration/doctor interface and invokes
  those same service functions.
- `setup.sh --non-interactive` remains stable for automation.
- The iOS app owns QR scanning, Keychain credentials, local iPhone speech, and
  per-Computer presentation.
