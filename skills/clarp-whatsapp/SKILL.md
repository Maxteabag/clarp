---
name: clarp-whatsapp
description: Read, search, or send messages through the user's locally paired wacli account. Use only when WhatsApp is explicitly involved.
---

# Clarp WhatsApp

Require `wacli` and use its currently paired account; never hardcode a phone
number. Sync before reading. Reading is non-mutating, but sending requires the
exact recipient and message to be authorized by the user.
