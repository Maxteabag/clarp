# Fleet map: vision and architecture decisions

Read [VISION.md](VISION.md) first. These records explain the owner's intent and
why the accepted design works the way it does. They are not a list of generated
ideas, a task backlog, or an approval queue.

## Current decisions

Optional camera following is specified in [0013](0013-follow-activity-camera.md).

| Record | Status | Decision |
| --- | --- | --- |
| [0001](0001-procedural-visual-software.md) | Accepted | Agents develop executable visual software, not just icons. |
| [0002](0002-compatible-evolution.md) | Accepted | Expand existing concepts; novelty does not authorize a new theme. |
| [0003](0003-separate-views.md) | Accepted | World and side-by-side Agent cabinets are separate views. |
| [0004](0004-agent-avatars.md) | Accepted | World agents use their existing Clarp portraits instead of boats. |
| [0005](0005-evidence-and-runtime.md) | Accepted | Real evidence, stable identity, deterministic rendering, isolated generation. |
| [0006](0006-work-at-real-destinations.md) | Accepted | Agents act at real files, directories and repositories; retire action-category stations. |
| [0007](0007-workflow-relevance-and-living-relations.md) | Accepted direction; encodings exploratory | Emphasize meaningful work, compact ownership regions, fluid interactions and persistent relationships. |
| [0008](0008-recognizable-identity-and-information-economy.md) | Accepted; sizing resolved by 0009 | Prefer recognizable identities and useful, nonredundant labels. |
| [0009](0009-overall-map-without-focus.md) | Accepted | Flow is an overall map; selection inspects. Uniform sizing superseded by 0011. |
| [0010](0010-mobile-map-navigation.md) | Accepted | Support phone and desktop viewports with touch navigation; remove the desktop size gate. |
| [0011](0011-interpretive-craft-and-logical-groups.md) | Accepted | Interpret the owner’s taste; craft logical groups, meaningful differences and visible workflows rather than literal file containers. |
| [0012](0012-visible-work-objects.md) | Accepted direction | Make work itself visible: one persistent object per recorded plan, with intent, attributed evidence and recorded outcome kept separate and honest. |

The combined current direction is **The Lantern Works with agent avatars**, plus
an optional **Agent cabinets** view. Preserve workshops, harbor, hierarchy and
meaningful activity at real destinations. ADR 0007 refines the direction toward
compact, fluid workflow visualization: filesystem structure supplies context,
while activity, relevant changes and persistent relationships receive emphasis.
Specific new layouts and animation encodings are still exploratory.

## How to use and maintain these records

- A new explicit owner instruction takes precedence over these records. If it
  changes a lasting decision, add a new numbered ADR explaining the context,
  decision, consequences and which earlier decision it supersedes. Update this
  index and the current vision as needed; retain earlier rationale as history.
- Do not treat a model's aesthetic choice as a new owner decision. Routine
  generated code revisions belong in the source library's decision history.
  They cannot silently override an accepted ADR.
- A new event, unfamiliar filename, or revision number is not a strong demand
  for a structural rewrite. Follow 0002 before changing familiar concepts.
- ADRs should capture durable intent, not transient test counts, deployment
  status, private event payloads, secrets or screenshots of one moment.
- When behavior and an ADR disagree, investigate the mismatch. Do not simply
  rewrite the ADR to legitimize the implementation.

`AGENTS.md` directs repository agents here. `world_prompt()` loads this directory
into every autonomous source-authoring request, including repair attempts.
The installer ships the same files, so deployed authors receive the same vision.
