---
name: clarp-self-prompt
description: Send a prompt to a Clarp agent immediately or on a schedule. Use for continuations, reminders, and durable agent wakeups.
---

# Clarp Self Prompt

Use the installed helper:

```bash
clarp-admin prompt --to SESSION --text "Continue the task"
clarp-admin prompt --to SESSION --text "Check again" --delay 30m
```

Target the intended session explicitly. Scheduled prompts are automation
messages, not messages authored by the user.
