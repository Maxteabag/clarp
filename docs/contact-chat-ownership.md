# Contact and Chat ownership

Clarp presents two related but distinct objects:

- A **Contact** is reusable identity: name, portrait, personality prompt, and
  default provider-aware voice. Editing a Contact affects future Chats. A
  built-in Contact may have a device-owned customization without mutating the
  shipped defaults.
- A **Chat** is one session on one Computer: backend, model/effort, workspace,
  queue, automation, teams, context, artifacts, images, transcript, and files.

The native Chat Profile reflects this boundary. Contact actions live in the
Contact card; session settings and content live under This Chat.

`Release Chat` is immediate and exact to `(Computer, session)`. It ends the
runtime, clears queued work and focus, removes the active route, and returns the
app to Chats. It does not delete the Contact, transcript, artifacts, or files.
Starting that Contact again mints a fresh session ID so retained history cannot
be resurrected accidentally.

`Remove Contact` deletes only a saved custom Contact definition. Existing Chat
rows retain their snapshotted identity and history. These actions must never
share labels, confirmation copy, or endpoint semantics.
