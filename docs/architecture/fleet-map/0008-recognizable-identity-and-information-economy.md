# 0008 — Recognizable identities and minimal redundant text

Status: Accepted direction; final sizing policy remains exploratory.

Date: 2026-09-06

Refines: 0007 (relevance and legible interactions), 0004 (existing portraits).

## Context

Peter asked why the focused Stickline workspace is much larger than nearby
workspaces and why the other agents appear so small. The Flow prototype expands
one selected workspace and compresses other workspaces into navigation previews.
It initially selects a workspace using recent activity, current occupants and
available file evidence. This is a focus/context display policy, not a statement
about storage size, intrinsic importance or the scale of the agents' actual work.
Its purpose was not sufficiently self-explanatory.

Peter also asked that future procedurally generated visuals minimize reading and
redundancy. His concrete examples were the GitHub logo in place of a GitHub text
heading, Maxteabag's actual profile image in place of a repeated owner name, and
avoiding a “JS” badge when the displayed filename already ends in “.js”.

## Decision

Prefer familiar, accurate graphical identity when it communicates the same fact
more intuitively than text:

- Use recognizable service marks, such as GitHub's logo, where appropriate.
- Use an owner's actual account/profile image or organization mark inside the
  service's ownership region. Do not infer account type from the image, invent a
  portrait, or substitute an unrelated agent's avatar.
- Preserve accurate names as accessible labels and on inspection. Provide a short
  textual fallback when a mark/image is unavailable or would be ambiguous.

Every visible label or symbol should contribute useful information. Avoid encoding
the same fact twice merely because two templates independently supply it. For
example, keep a useful full filename such as `office-map.js` and normally omit a
second textual “JS” badge. Removing the filename and leaving an ambiguous icon
would not satisfy the intent. Use parent context or inspection to distinguish
identical filenames when needed.

Text is allowed and valuable for names, filenames, disambiguation, uncertainty,
errors and details that graphics cannot reliably communicate. The goal is to
reduce reading effort while retaining information, not to remove all text or
maximize unexplained symbols. Actions should primarily read through behavior;
labels should add identity or clarification rather than narrate every motion.

Scaling should not imply an unsupported measure. If focus changes display size,
make that interaction discoverable. Tiny context avatars must not silently hide
important work or make the overall activity seem less significant. Local
checkouts and remote repositories may represent distinct objects for the same
project; preserve their relationship and make their different roles understandable.

## Consequences and open work

This guides future compatible visual improvements. It does not prescribe a new
full-screen layout, a universal icon vocabulary, or a new automatic size/importance
formula. A more balanced overview versus the current focus lens remains a design
question to test. Do not treat the user's sizing question as approval for another
wholesale redesign.

Resolve actual assets through the Host/client asset pipeline, cache them outside
frames, and keep the source sandbox network-disabled. Preserve existing views,
accessibility and truthful fallbacks. Test recognition at normal viewing scale,
including missing portraits, unfamiliar services and duplicate filenames.
