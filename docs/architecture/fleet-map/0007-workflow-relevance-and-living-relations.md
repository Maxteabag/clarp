# 0007 — Organize the living map around relevant work and relationships

Status: Accepted direction; specific visual encodings remain exploratory.

Date: 2026-09-06

Refines: 0006 (correct destinations do not require travel for every operation),
0002 (compatible growth), and 0005 (stable identity and truthful evidence).

## Context

Peter values seeing agents at real places and understanding filesystem proximity,
but the current grid of oversized workspace/folder rectangles reads as a static
catalogue. It makes location visible without making current work, meaningful
changes, relationships and repeated patterns sufficiently intuitive.

He asked to rethink what information matters to the viewer, make interactions
visually legible, show persistent relations, use more fluid motion, and reflect
natural ownership. In particular, repositories belonging to Maxteabag should
visibly belong to that owner within GitHub. An owner may be a personal account or
an organization; do not mislabel its type without evidence.

## Accepted principles

### 1. Relevance determines emphasis

The overview should help answer:

- Who is doing what, and to which actual thing?
- What has materially changed, and where is attention needed?
- Which systems, artifacts and agents are connected?
- What workflow is unfolding, and which interactions recur over time?

When task intent is explicitly recorded, connect the current action and result
to that intent. Do not infer an agent's objective or progress from a shell verb
alone. A useful picture relates intent, observed action and evidenced outcome.

Filesystem structure remains useful context. It should not automatically consume
most of the canvas or assign every directory equal prominence. A complete model
can have a selective overview: keep quiet detail accessible through exploration,
not permanently expanded into giant empty rooms.

Distinguish three forms of novelty: a new recorded event, a new structural entity
or relationship, and new information that matters to the person. None alone is
automatic authorization for a new visual theme. Repeated reads may reuse existing
motion; a new integration or a meaningful result may deserve stronger emphasis.

### 2. Show meaningful containment and multiple relationship types

Reflect observed ownership and containment, for example:
GitHub → Maxteabag → repositories → relevant resources/activity.
Support additional owners when they appear. Do not wait for a second owner to
represent the first one truthfully. A local checkout can belong to a filesystem
region and connect to its remote repository; do not force every relationship
into a single parent-child tree.

Separate durable facts (ownership, directory containment, configured remotes,
verified integrations) from recent activity and possible patterns. A configured
origin does not prove a transfer occurred. Shared targets do not prove agents
communicated. Inferred patterns must remain distinguishable from observed links.

### 3. Make action understandable primarily through behavior

Use local effects, motion, transformation and directional interaction so the
viewer can understand work without reading a running transcript or count badges.
Show a file being created/deleted, an edit occurring, a transfer taking place,
a query reaching a service, or a problem interrupting a workflow where supported
by evidence. Start, progress, completion and failure should look different.

Text remains useful for identity, clarification, inspection and accessibility.
It must not carry the entire explanation of an otherwise static scene. Do not
replace understanding with “changed N times” or “last updated at” as the primary
visual mechanism.

### 4. Movement is expressive, not compulsory locomotion

Avatars may relocate when their meaningful work context changes. Fine-grained or
rapid interactions may instead use a beam, ray, tether, reach or local effect at
the actual target. This qualifies 0006: the target must be correct, but an avatar
does not need to traverse the map for every read or write.

A burst across nearby files should feel like coherent work, not frantic hopping.
Keep portraits and the established world identity. Any new motion should add
meaning rather than arbitrary noise.

### 5. Make the world fluid while preserving spatial memory

Avoid a rigid rectangular grid and disproportionate empty workspace areas.
Explore compact, soft-edged, nested regions and gently responsive placement.
Allow restrained bounce, settling and local motion while keeping meaningful
places recognizable. Stable identity does not require absolutely motionless
coordinates, and fluidity does not justify reshuffling the whole map repeatedly.
Respect reduced motion and the user's pan/zoom/selection.

### 6. Preserve relationships and legible traces over time

The viewer should be able to spot recurring interactions and workflows, not
just watch isolated tool calls disappear. Durable relations should survive gaps
in recent activity. Recent changes may leave a fading visual trace; repeated
observed interactions may gradually reveal a familiar route or cluster.

Keep structural persistence distinct from activity intensity. Repetition alone
is not importance, a causal dependency, successful completion or coordination.
History should help the viewer recognize patterns without permanently saturating
the canvas or drawing an unreadable web of every past event.

## Open design work — not yet chosen

The owner is still exploring the design. This ADR accepts the goals above, not a
final layout algorithm, animation vocabulary, scoring formula, retention period
or wholesale renderer replacement. In particular, ownership regions, beams,
change traces and fluid positioning need concrete visual trials.

A useful next trial should let a viewer recognize a read, an edit, a confirmed
creation/deletion, a transfer, a failure and repeated work with action text hidden.
Test with real representative workflows, including research/API activity and
unknown destinations. Compare compactness and clarity at normal viewing scale,
not only a zoomed-out screenshot. Keep previous versions available.

This direction should guide compatible expansions and deliberate focused
experiments. It does not repeal 0002 or authorize spontaneous global redesigns.


## First authorized prototype

Peter approved building the initial compact workflow trial. It is offered as the
optional **Flow** view; World and Agent cabinets remain available. The prototype
focuses a workspace, shows nearby files with local interaction effects, nests
GitHub repositories inside their observed owners, and retains browser-local
structural relationship/touch memory. Quiet workspaces are compact and selectable.
Action labels are optional and initially hidden.

A separately labeled, synthetic Workflow demo exercises two roles through reading,
editing, a failed test, repair, successful testing, creation, deletion, commit and
push. It never enters the Host activity stream or invokes the learning pipeline.
The chosen layouts, focus limits and animation encodings are prototype choices,
not new permanent artistic constraints. Evaluate clarity and usefulness before
promoting or extending the view. Automatic source evolution still targets World;
it does not silently rewrite this experiment or replace other views.
