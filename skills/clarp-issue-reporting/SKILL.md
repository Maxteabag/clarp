---
name: clarp-issue-reporting
description: Detect and report Clarp bugs, regressions, confusing behavior, and feature requests. Use when a user encounters trouble with Clarp, wants Clarp improved, or an agent fixes or works around a likely Clarp product problem. Search Maxteabag/clarp before proposing a new issue. Do not use for unrelated projects merely accessed through Clarp.
---

# Clarp issue reporting

Help with the immediate problem first. Do not interrupt recovery work merely to
open an issue. Once the user is unblocked, decide whether the experience exposes
a reusable Clarp bug or product opportunity rather than a one-off local mistake.

Suggest reporting when Clarp itself has unexpected behavior, recurring friction,
a misleading message, a missing capability, or a workaround or source fix that
would help other users. Also suggest it after diagnosing or fixing such a problem
without the user explicitly calling it a bug. Skip transient provider outages,
usage limits, user mistakes, private customization, and defects in another
repository or service.

## Search before writing

Search open and closed issues and pull requests using short, non-sensitive terms
from the component, symptom, and error. Prefer authenticated GitHub CLI:

```bash
gh issue list --repo Maxteabag/clarp --state all --search "<terms>" --limit 20 \
  --json number,title,state,url,labels,updatedAt
gh pr list --repo Maxteabag/clarp --state all --search "<terms>" --limit 20 \
  --json number,title,state,url,updatedAt
```

Try another query when terminology may differ. Never put credentials, private
paths, user identities, transcript text, or other sensitive values in a public
search.

- Matching open issue: do not duplicate it. Offer a comment only when the new
  reproduction, evidence, workaround, or fix adds useful information.
- Matching closed issue or merged fix: determine whether this is the same cause,
  a regression, or only a similar symptom. Propose a linked regression issue when
  a new report is clearer than reopening the old discussion.
- No meaningful match: draft a new bug or feature request.

## Draft an actionable report

Classify a **bug** when observed behavior violates expected behavior. Classify a
**feature request** when current behavior is understood but a broadly useful
capability or workflow is missing.

For bugs, capture only verified facts:

- concise symptom and expected behavior;
- minimal reproduction and frequency;
- Clarp version or commit, platform, client, and agent backend when relevant;
- useful diagnostics with secrets and personal data removed;
- impact and current workaround;
- root cause only when evidence establishes it.

For feature requests, describe the user problem, affected workflow, desired
outcome, why it likely helps other users, and a few observable acceptance
examples. Keep implementation ideas separate from requirements.

When the agent or user already fixed the problem, preserve that result instead
of filing a vague historical note. Search first, then draft either a useful
comment on the existing issue or a retrospective bug report containing:

- original symptom and reproduction;
- proven root cause;
- exact fix and its scope;
- tests or live verification actually run;
- public commit or pull-request link when one exists;
- whether the fix is local, proposed, merged, or released.

Do not claim a local fix shipped. Do not create an issue solely to advertise a
patch when no reusable Clarp problem exists.

## Approval and publication

For a proactively detected report, show the proposed target, title, body, label,
and any duplicate or related issue. Ask for explicit approval immediately before
creating the issue or comment. A direct instruction to post a concrete report is
already approval; a general complaint is not. Respect a refusal and do not keep
suggesting the same report.

Use `bug` or `enhancement` only when that label exists in the repository. Write
the approved body to a secure temporary file so shell interpolation cannot alter
it, then use one of:

```bash
gh issue create --repo Maxteabag/clarp --title "<title>" --body-file <file> --label <label>
gh issue comment <number> --repo Maxteabag/clarp --body-file <file>
```

An equivalent authenticated GitHub integration is acceptable. If no write-capable
authenticated tool is available, return the complete draft and state that nothing
was posted. Never request or expose a GitHub token in chat.

After posting, read back the issue or comment, verify its URL and visible content,
and give the user the link. Creating an issue does not authorize source changes,
a pull request, assignment, milestones, release work, or merging.
