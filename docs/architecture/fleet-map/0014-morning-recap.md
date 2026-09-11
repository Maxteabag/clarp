# 0014 — Catch me up

Status: Accepted. Owner request, 2026-09-11.

The owner wants to remember recent work and see what agents produced after
time away. Add an optional recap within the map, organized by stable agent
identity with all returned published artifacts visible. Preserve the existing
map as the complementary spatial view; changing modes does not redesign it.

Offer Yesterday (local calendar), Since last review and Last 7 days. Include
the user's last recorded request, agent-declared task status and current pending
decisions. Published outcomes are distinct from declared completion. Open tasks
and pending decisions remain visible even without new activity after a review.
Archived agents keep their identity.
Do not invent project attribution or treat unpublished filesystem files as a
complete artifact gallery. State coverage and record limits visibly.

Open recorded media and external HTTPS deliverables; read documents in an inert
reader and HTML forms in a restricted, read-only preview. Load large content only
when requested. Never submit a form or answer a decision from a recap preview.

Remember reviewed artifact versions locally. Opening an artifact or explicitly
marking it reviewed records its version; later updates become unseen again.
Only I'm caught up advances the review boundary, and never for truncated or
failed results. Storage failure leaves the recap usable and explains that marks
will not survive the visit. The recap endpoint is authenticated by the Host's
normal routing and performs no writes or provider calls.
