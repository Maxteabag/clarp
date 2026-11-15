# Differences from the Python Host, on purpose

The Rust Host joins the fleet on the contract the apps already speak. Where it differs from the
Python Host in something a client can see, the difference is in this file with its reason, or it
is a bug. The conformance run (`../conformance`) finds the differences; this file explains the
ones that stay.

## Features that are not there yet

The Rust Host leaves a feature it does not have out of `capabilities.features`, which is how an
app learns not to expect it. What belongs to such a feature is left out of both sides before the
conformance run compares them (`NOT_YET` in `conformance/conform.py`).

| Feature | What is absent | Planned |
|---|---|---|
| `send_delivery_state` | `/log`: `delivery_scope`, `delivery_stop_generation`, and `delivery` on a user row; the event `delivery-updated` | Stage 3 |

Everything else in the Python Host's `capabilities.features` is absent too; the conformance
corpus does not reach it yet. The list grows as the corpus does.

## Compared as what a client may rely on

| What | The Python Host | The Rust Host | Why it is compared this way |
|---|---|---|---|
| Revisions | a counter that grows with every write of a row | the log position times 2^20, with room for a reply that grows | The contract promises an order, not numbers. The run compares each revision as its place among the revisions of its capture |
| The Host's version in `server-version` | the newest change time of its static files | its own version | Each Host's own. The run compares that there is one |

## Differences that are registered one by one

Each has its id in `../conformance/intended.txt`. The id is a hash of the place and both values:
it covers that exact difference and nothing else.

| What | The Python Host | The Rust Host | Why |
|---|---|---|---|
