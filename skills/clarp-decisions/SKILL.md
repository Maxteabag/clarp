---
name: clarp-decisions
description: Request a durable yes or no decision when the user must authorize an action.
---
# Decisions
Use `clarp-agent-artifacts decision "$CLAUDE_PWA_SESSION" TITLE QUESTION Yes No JSON_PAYLOAD`.
Decision buttons are always the literal labels `Yes` and `No`; put the action
and consequences in the question and payload rather than custom button text.
Do not perform the protected action until the accepted result returns. Record consequences and relevant identifiers in the payload.
