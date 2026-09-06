# Spark: current low vs refined low vs refined medium

36 fresh calls; three repetitions per condition; four identical synthetic activities. Same model gpt-5.3-codex-spark. Refined low and medium use identical instructions. Developer bypass verified separately: no model calls. No production changes.

| Level | Current low | Refined low | Refined medium |
|---|---:|---:|---:|
| Technical | 3.87 s | 3.78 s | 3.32 s |
| Balanced | 3.47 s | 3.78 s | 3.72 s |
| Plain English | 3.64 s | 3.77 s | 3.47 s |
| Grandma | 3.52 s | 3.77 s | 4.07 s |

Times are median batch completion, not per-item latency. Small pilot, not a P95/SLA.

Format/160-character checks passed: 33/36 batches. All attempts retained.

Primary tables consistently show repetition zero, not the best-looking answer.

## Interpretation and limitations

- The first 33 samples completed before a user-requested design investigation;
  the final three (Grandma repetition two, all columns) ran after resumption.
  Completed samples, including failures, were not rerun. This interruption limits
  the timing comparison further; conditions were rotated within each block.
- Medium preserved the broad unpaid-and-overdue filtering and read-versus-run
  distinction in these cases. It did not reliably make Plain English or Grandma
  simpler. Its Grandma repetition two included overdue_invoices.py,
  network/client.ts, and task_zeta.sh despite the prompt requesting everyday terms.
- Three batches failed the lab's 160-character limit: refined Technical low
  repetition one, and refined Technical medium repetitions one and two.
  Responses were otherwise available and retained. This lab limit is stricter
  than the Host's 240-character acceptance limit; these are not HTTP failures.
- Medium's median was lower than refined-low at Technical, Balanced and Plain
  English, higher at Grandma. With only three samples and variable provider
  latency, this does not prove medium is faster or predict production performance.
- No overall quality winner or production model change is justified here.
  Exact combined instructions, effort, usage and outputs are retained in
  results/spark-three-columns.jsonl. Developer is raw in all columns.

## Find overdue invoices

| Level | Current low | Refined low | Refined medium |
|---|---|---|---|
| Developer | `python overdue_invoices.py` | Same raw activity | Same raw activity |
| Technical | Runs overdue_invoices.py to read invoices.csv, check unpaid items against today, and output overdue invoice IDs, customers, and amounts. | Run overdue_invoices.py to read invoices.csv and print invoice_number, customer, and amount for rows where paid is false and due_date is before today. | Run overdue_invoices.py to read invoices.csv and print invoice_number, customer, and amount for rows where paid is false and due_date is before today. |
| Balanced | Runs an overdue-invoice check utility that reads invoice records and prints unpaid items whose due dates are before today. | Run overdue_invoices.py to scan invoices.csv and print invoice number, customer, and amount for unpaid rows whose due_date is before today. | Run the invoice script to scan invoices.csv and print unpaid invoices with due dates before today, including invoice id, customer, and amount. |
| Plain English | The user runs a local overdue-invoice check to print unpaid invoices whose due date is already in the past. | Reads each invoice row and prints invoice number, customer and amount for unpaid items due before today. | Run a script that reads invoice records and lists unpaid entries with due dates before today, printing invoice number, customer, and amount. |
| Grandma | Runs a script that lists unpaid invoices whose due date is already in the past. | Run an invoice check and print invoice number, customer, and amount for unpaid overdue items. | Run invoice check for unpaid, past-due entries and show invoice number, customer, and amount. |

## Inspect invoice checker

| Level | Current low | Refined low | Refined medium |
|---|---|---|---|
| Developer | `sed -n '1,80p' overdue_invoices.py` | Same raw activity | Same raw activity |
| Technical | Runs sed -n '1,80p' on overdue_invoices.py to inspect the script’s top section and review its overdue-filter logic. | Read overdue_invoices.py with sed -n '1,80p' to inspect logic that outputs invoice_number, customer, and amount for unpaid rows with past due_date. | Read overdue_invoices.py with sed -n to inspect its line logic that filters CSV rows where paid==false and due_date is earlier than today. |
| Balanced | Views the start of the invoice-processing script to inspect the logic that selects unpaid, past-due entries. | Inspect overdue_invoices.py source (read-only); it opens invoices.csv and prints invoice number/customer/amount for unpaid invoices that are past due. | Read the first part of overdue_invoices.py to inspect its overdue-filtering logic; this is a source view, not execution. |
| Plain English | The user reads the first part of a script to verify how it loads invoice records and filters for unpaid, overdue items. | Reads the script source to inspect how it checks invoice fields and overdue dates; no execution happens. | Read script source to inspect the invoice-check logic and confirm it filters for unpaid, past-due rows. |
| Grandma | Displays the first part of the invoice script to inspect its overdue unpaid-check logic. | Read source to learn how unpaid overdue invoice entries are selected and printed. | Read the file’s first lines to check how the invoice due-date check works. |

## Change waiting limit

| Level | Current low | Refined low | Refined medium |
|---|---|---|---|
| Developer | `Change request timeout from 45000 ms to 15000 ms.` | Same raw activity | Same raw activity |
| Technical | Edits the network client configuration to reduce the request timeout from 45,000 ms to 15,000 ms. | Edit network/client.ts to change the request timeout setting from 45000 ms to 15000 ms. | Edit network/client.ts to set the request timeout value to 15000 ms instead of 45000 ms. |
| Balanced | Edits the network client to reduce the request timeout setting from 45,000 ms to 15,000 ms. | Edit network/client.ts to change the request timeout setting from 45000 ms to 15000 ms. | Edit network/client.ts to change request timeout from 45,000 ms to 15,000 ms. |
| Plain English | The user edits a network client setting to reduce the request timeout from 45 seconds to 15 seconds. | Changes a request timeout setting from 45,000 ms to 15,000 ms. | Edit a configuration to shorten a request wait period from 45,000 ms (45 seconds) to 15,000 ms (15 seconds). |
| Grandma | Reduces the outgoing request timeout to 15 seconds from 45 seconds. | Edit network wait setting, reducing the allowed wait period from 45 to 15 seconds. | Edit settings to lower how long each request waits, from 45 seconds to 15 seconds. |

## Unknown task

| Level | Current low | Refined low | Refined medium |
|---|---|---|---|
| Developer | `bash task_zeta.sh` | Same raw activity | Same raw activity |
| Technical | Executes task_zeta.sh as a local shell task, carrying out whatever actions are encoded in that script. | Run task_zeta.sh; its purpose is unknown from the available evidence. | Run task_zeta.sh, but its purpose and effect are unknown from the available evidence. |
| Balanced | Runs a local task script named task_zeta.sh; the actual task goal is not specified in the provided data. | Run task_zeta.sh; its purpose is unknown from the provided evidence. | Run task_zeta.sh; the script’s purpose is unknown from the provided evidence, so intent and effects are not established. |
| Plain English | The user runs an existing shell task script to execute its predefined automated routine; the exact routine is not specified. | Runs a shell script, but its purpose is unknown from the available evidence. | Run a shell script; its purpose is unknown from the provided evidence. |
| Grandma | Executes a local task script, but its specific action is not provided here. | Run a shell task, but its purpose is unknown from supplied evidence. | Start a task whose purpose is not yet clear. |

## Every repetition

### Technical / refined / low / repetition 0

- **Find overdue invoices:** Run overdue_invoices.py to read invoices.csv and print invoice_number, customer, and amount for rows where paid is false and due_date is before today.
- **Inspect invoice checker:** Read overdue_invoices.py with sed -n '1,80p' to inspect logic that outputs invoice_number, customer, and amount for unpaid rows with past due_date.
- **Change waiting limit:** Edit network/client.ts to change the request timeout setting from 45000 ms to 15000 ms.
- **Unknown task:** Run task_zeta.sh; its purpose is unknown from the available evidence.

### Technical / refined / medium / repetition 0

- **Find overdue invoices:** Run overdue_invoices.py to read invoices.csv and print invoice_number, customer, and amount for rows where paid is false and due_date is before today.
- **Inspect invoice checker:** Read overdue_invoices.py with sed -n to inspect its line logic that filters CSV rows where paid==false and due_date is earlier than today.
- **Change waiting limit:** Edit network/client.ts to set the request timeout value to 15000 ms instead of 45000 ms.
- **Unknown task:** Run task_zeta.sh, but its purpose and effect are unknown from the available evidence.

### Technical / baseline / low / repetition 0

- **Find overdue invoices:** Runs overdue_invoices.py to read invoices.csv, check unpaid items against today, and output overdue invoice IDs, customers, and amounts.
- **Inspect invoice checker:** Runs sed -n '1,80p' on overdue_invoices.py to inspect the script’s top section and review its overdue-filter logic.
- **Change waiting limit:** Edits the network client configuration to reduce the request timeout from 45,000 ms to 15,000 ms.
- **Unknown task:** Executes task_zeta.sh as a local shell task, carrying out whatever actions are encoded in that script.

### Balanced / refined / medium / repetition 0

- **Find overdue invoices:** Run the invoice script to scan invoices.csv and print unpaid invoices with due dates before today, including invoice id, customer, and amount.
- **Inspect invoice checker:** Read the first part of overdue_invoices.py to inspect its overdue-filtering logic; this is a source view, not execution.
- **Change waiting limit:** Edit network/client.ts to change request timeout from 45,000 ms to 15,000 ms.
- **Unknown task:** Run task_zeta.sh; the script’s purpose is unknown from the provided evidence, so intent and effects are not established.

### Balanced / baseline / low / repetition 0

- **Find overdue invoices:** Runs an overdue-invoice check utility that reads invoice records and prints unpaid items whose due dates are before today.
- **Inspect invoice checker:** Views the start of the invoice-processing script to inspect the logic that selects unpaid, past-due entries.
- **Change waiting limit:** Edits the network client to reduce the request timeout setting from 45,000 ms to 15,000 ms.
- **Unknown task:** Runs a local task script named task_zeta.sh; the actual task goal is not specified in the provided data.

### Balanced / refined / low / repetition 0

- **Find overdue invoices:** Run overdue_invoices.py to scan invoices.csv and print invoice number, customer, and amount for unpaid rows whose due_date is before today.
- **Inspect invoice checker:** Inspect overdue_invoices.py source (read-only); it opens invoices.csv and prints invoice number/customer/amount for unpaid invoices that are past due.
- **Change waiting limit:** Edit network/client.ts to change the request timeout setting from 45000 ms to 15000 ms.
- **Unknown task:** Run task_zeta.sh; its purpose is unknown from the provided evidence.

### Plain English / baseline / low / repetition 0

- **Find overdue invoices:** The user runs a local overdue-invoice check to print unpaid invoices whose due date is already in the past.
- **Inspect invoice checker:** The user reads the first part of a script to verify how it loads invoice records and filters for unpaid, overdue items.
- **Change waiting limit:** The user edits a network client setting to reduce the request timeout from 45 seconds to 15 seconds.
- **Unknown task:** The user runs an existing shell task script to execute its predefined automated routine; the exact routine is not specified.

### Plain English / refined / low / repetition 0

- **Find overdue invoices:** Reads each invoice row and prints invoice number, customer and amount for unpaid items due before today.
- **Inspect invoice checker:** Reads the script source to inspect how it checks invoice fields and overdue dates; no execution happens.
- **Change waiting limit:** Changes a request timeout setting from 45,000 ms to 15,000 ms.
- **Unknown task:** Runs a shell script, but its purpose is unknown from the available evidence.

### Plain English / refined / medium / repetition 0

- **Find overdue invoices:** Run a script that reads invoice records and lists unpaid entries with due dates before today, printing invoice number, customer, and amount.
- **Inspect invoice checker:** Read script source to inspect the invoice-check logic and confirm it filters for unpaid, past-due rows.
- **Change waiting limit:** Edit a configuration to shorten a request wait period from 45,000 ms (45 seconds) to 15,000 ms (15 seconds).
- **Unknown task:** Run a shell script; its purpose is unknown from the provided evidence.

### Grandma / refined / low / repetition 0

- **Find overdue invoices:** Run an invoice check and print invoice number, customer, and amount for unpaid overdue items.
- **Inspect invoice checker:** Read source to learn how unpaid overdue invoice entries are selected and printed.
- **Change waiting limit:** Edit network wait setting, reducing the allowed wait period from 45 to 15 seconds.
- **Unknown task:** Run a shell task, but its purpose is unknown from supplied evidence.

### Grandma / refined / medium / repetition 0

- **Find overdue invoices:** Run invoice check for unpaid, past-due entries and show invoice number, customer, and amount.
- **Inspect invoice checker:** Read the file’s first lines to check how the invoice due-date check works.
- **Change waiting limit:** Edit settings to lower how long each request waits, from 45 seconds to 15 seconds.
- **Unknown task:** Start a task whose purpose is not yet clear.

### Grandma / baseline / low / repetition 0

- **Find overdue invoices:** Runs a script that lists unpaid invoices whose due date is already in the past.
- **Inspect invoice checker:** Displays the first part of the invoice script to inspect its overdue unpaid-check logic.
- **Change waiting limit:** Reduces the outgoing request timeout to 15 seconds from 45 seconds.
- **Unknown task:** Executes a local task script, but its specific action is not provided here.

### Technical / refined / medium / repetition 1

- **Find overdue invoices:** Run overdue_invoices.py; it reads invoices.csv and prints invoice_number, customer, and amount for each row with paid=='false' and due_date earlier than today.
- **Inspect invoice checker:** Read overdue_invoices.py with sed; the visible source filters unpaid rows where paid=='false' and due_date < today, then prints invoice_number, customer, amount.
- **Change waiting limit:** Edit network/client.ts to change the request timeout from 45000 ms to 15000 ms.
- **Unknown task:** Run bash task_zeta.sh; its purpose is unknown from the available evidence.

### Technical / baseline / low / repetition 1

- **Find overdue invoices:** This runs `python overdue_invoices.py`; it reads `invoices.csv`, selects unpaid invoices past due today’s date, and prints invoice number, customer, and amount.
- **Inspect invoice checker:** This runs `sed -n '1,80p' overdue_invoices.py` to read the first 80 lines of the file for inspection, without executing it.
- **Change waiting limit:** This edits `network/client.ts`, changing request timeout from 45000 ms to 15000 ms to cut client wait time from 45s to 15s on slow responses.
- **Unknown task:** This runs `bash task_zeta.sh`, invoking that shell script; its exact intent is unknown from the available context.

### Technical / refined / low / repetition 1

- **Find overdue invoices:** Run overdue_invoices.py: it reads invoices.csv, filters rows where paid == 'false' and due_date is before today, and prints each invoice_number, customer, and amount.
- **Inspect invoice checker:** Read overdue_invoices.py with sed to inspect its logic: it checks paid=='false' and due_date < date.today(), then prints invoice_number, customer, and amount.
- **Change waiting limit:** Edit network/client.ts to change the request timeout setting from 45000 ms to 15000 ms.
- **Unknown task:** Run task_zeta.sh; its purpose is unknown from the available evidence.

### Balanced / baseline / low / repetition 1

- **Find overdue invoices:** Runs an overdue-invoice check that scans a CSV of invoices and outputs unpaid records with due dates already passed.
- **Inspect invoice checker:** Reads the first part of a file to inspect its invoice-processing logic and verify how overdue unpaid items are filtered.
- **Change waiting limit:** Edits a network client setting to shorten request timeout from 45,000 ms to 15,000 ms so slow calls fail sooner.
- **Unknown task:** Runs a local shell task, but the business purpose of that task is not provided in the available activity data.

### Balanced / refined / low / repetition 1

- **Find overdue invoices:** Run overdue_invoices.py to read invoices, select unpaid entries whose due date is before today, and print each invoice’s number, customer, and amount.
- **Inspect invoice checker:** Inspect overdue_invoices.py source to verify it reads a CSV file, checks for unpaid overdue records, and prints selected invoice details.
- **Change waiting limit:** Update network/client.ts to reduce request timeout from 45,000 ms to 15,000 ms.
- **Unknown task:** Run task_zeta.sh. The script’s purpose is unknown from supplied evidence; only that it was invoked is shown.

### Balanced / refined / medium / repetition 1

- **Find overdue invoices:** Run overdue_invoices.py to print invoice number, customer, and amount for invoices marked unpaid with a due_date before today.
- **Inspect invoice checker:** Read overdue_invoices.py source with sed to inspect its logic for filtering invoices.csv by unpaid and past-due status.
- **Change waiting limit:** Edit network/client.ts to change the request timeout setting from 45000 ms to 15000 ms.
- **Unknown task:** Run bash task_zeta.sh; the script’s purpose is unknown from the supplied evidence.

### Plain English / refined / low / repetition 1

- **Find overdue invoices:** Runs a local script that reads invoice records and lists invoice number, customer, and amount for unpaid items past their due date.
- **Inspect invoice checker:** Reads the script’s source lines only. This is inspection of content and does not run anything or change data.
- **Change waiting limit:** Edits a setting to shorten request waiting time from 45,000 ms to 15,000 ms.
- **Unknown task:** Runs a local task script. Purpose is unknown from supplied evidence, so its exact action is not verified.

### Plain English / refined / medium / repetition 1

- **Find overdue invoices:** Runs the invoice-checking script to list invoice number, customer and amount for items still unpaid and past their due date.
- **Inspect invoice checker:** Reads the first part of the invoice-checking script to inspect how it detects unpaid, overdue entries.
- **Change waiting limit:** Changes a network waiting-for-a-reply limit from 45,000 ms down to 15,000 ms.
- **Unknown task:** Runs a shell script with no provided details, so its purpose is unknown.

### Plain English / baseline / low / repetition 1

- **Find overdue invoices:** Runs a script that scans invoice records and prints unpaid invoices whose due date is already past.
- **Inspect invoice checker:** Opens the invoice script to review its first section and verify how unpaid overdue invoices are selected.
- **Change waiting limit:** Edits the client code to lower the network request timeout from 45,000 ms to 15,000 ms.
- **Unknown task:** Runs a task script named task_zeta.sh; the exact operation is unknown from the provided details.

### Grandma / refined / medium / repetition 1

- **Find overdue invoices:** Run a check that prints unpaid invoices whose due date already passed.
- **Inspect invoice checker:** Read the source to learn how unpaid overdue invoices are selected.
- **Change waiting limit:** Lower the wait period on requests from forty-five to fifteen seconds.
- **Unknown task:** Start a task whose purpose is not yet clear.

### Grandma / baseline / low / repetition 1

- **Find overdue invoices:** The command checks invoice records and prints unpaid ones whose due dates are already past.
- **Inspect invoice checker:** The command inspects the invoice-check script logic for deciding which records are overdue and unpaid.
- **Change waiting limit:** This edits client request settings to shorten timeouts from 45 seconds to 15 seconds.
- **Unknown task:** This runs a custom task script, but its specific purpose is not shown.

### Grandma / refined / low / repetition 1

- **Find overdue invoices:** Run invoice check to print invoice number, customer and amount for unpaid overdue bills.
- **Inspect invoice checker:** Read the invoice-check source to verify it prints unpaid items past due date.
- **Change waiting limit:** Shorten network wait from 45 to 15 seconds before giving up on response.
- **Unknown task:** Start a task whose purpose is not yet clear.

### Technical / baseline / low / repetition 2

- **Find overdue invoices:** Runs `python overdue_invoices.py`, executing a local check that selects and prints unpaid invoices with past due dates.
- **Inspect invoice checker:** Runs `sed -n '1,80p' overdue_invoices.py` to read the first 80 lines of the file for inspection.
- **Change waiting limit:** Edits `network/client.ts`, lowering request timeout from 45,000 ms to 15,000 ms in the client settings.
- **Unknown task:** Runs `bash task_zeta.sh`; its purpose is not specified in the provided data, so the exact operation is unknown.

### Technical / refined / low / repetition 2

- **Find overdue invoices:** Run overdue_invoices.py to load invoices.csv, select rows where paid='false' and due_date is before today, then print invoice_number, customer, and amount.
- **Inspect invoice checker:** Read overdue_invoices.py with sed to inspect its logic for filtering unpaid invoices with past due_date and printing invoice_number, customer, and amount.
- **Change waiting limit:** Edit network/client.ts to change the request timeout setting from 45000 ms to 15000 ms.
- **Unknown task:** Run bash task_zeta.sh; its purpose is unknown from the provided evidence.

### Technical / refined / medium / repetition 2

- **Find overdue invoices:** Run overdue_invoices.py to read invoices.csv, filter rows where paid is 'false' and due_date is earlier than today, then print invoice_number, customer, and amount.
- **Inspect invoice checker:** Read lines 1-80 of overdue_invoices.py with sed to inspect its overdue-invoice filtering logic without running it.
- **Change waiting limit:** Edit network/client.ts, changing the request timeout value from 45000 ms to 15000 ms.
- **Unknown task:** Run task_zeta.sh; its purpose is unknown from the available evidence.

### Balanced / refined / low / repetition 2

- **Find overdue invoices:** Run overdue_invoices.py to read invoices.csv, filter unpaid rows with due_date before today, and print invoice number, customer, and amount.
- **Inspect invoice checker:** Read overdue_invoices.py source (no execution); it contains logic that finds unpaid invoices past due date and prints selected invoice details.
- **Change waiting limit:** Edit network/client.ts to reduce request timeout from 45,000 ms to 15,000 ms.
- **Unknown task:** Run task_zeta.sh; the script’s purpose is unknown from supplied evidence.

### Balanced / refined / medium / repetition 2

- **Find overdue invoices:** Run overdue_invoices.py to scan invoices.csv and print invoice number, customer, and amount for unpaid rows with a past due date.
- **Inspect invoice checker:** Read overdue_invoices.py source using sed, showing the first 80 lines; this is inspection only, not execution.
- **Change waiting limit:** Edit network/client.ts to lower the request timeout setting from 45000 ms to 15000 ms.
- **Unknown task:** Run task_zeta.sh; its overall purpose is unknown from the provided evidence.

### Balanced / baseline / low / repetition 2

- **Find overdue invoices:** Runs a local script to read invoice records and list unpaid bills whose due date is before today.
- **Inspect invoice checker:** Views the beginning of a billing script to inspect its existing logic and behavior.
- **Change waiting limit:** Edits a network client setting to lower the request timeout from 45 seconds to 15 seconds.
- **Unknown task:** Launches a local shell task script to carry out its predefined workflow; its exact purpose is not specified.

### Plain English / refined / medium / repetition 2

- **Find overdue invoices:** Scan invoice records, list unpaid items with due dates before today, and print invoice number, customer, and amount.
- **Inspect invoice checker:** Read the script source to verify how invoice filtering is implemented; no code is run in this step.
- **Change waiting limit:** Lower request waiting-for-reply time from 45,000 ms to 15,000 ms.
- **Unknown task:** Run an unknown task script; its purpose is unknown from available evidence.

### Plain English / baseline / low / repetition 2

- **Find overdue invoices:** Run a check that scans invoice records and outputs unpaid invoices whose due date has already passed, including customer and amount.
- **Inspect invoice checker:** Inspect the start of a script to review how it identifies overdue unpaid invoices from stored invoice records.
- **Change waiting limit:** Edit a network request setting to shorten the timeout from 45 seconds to 15 seconds.
- **Unknown task:** Execute a custom shell task; the specific purpose of this task is not provided in the available details.

### Plain English / refined / low / repetition 2

- **Find overdue invoices:** Running script that reads the invoice list, selects unpaid rows with due dates before today, and prints invoice number, customer, and amount.
- **Inspect invoice checker:** Reading code that scans invoice records, checks for unpaid status and overdue dates, and outputs invoice number, customer, and amount.
- **Change waiting limit:** Changing the client waiting-for-reply time from 45,000 ms to 15,000 ms (a shorter timeout).
- **Unknown task:** Running a script whose purpose is not known yet, because its contents are not provided.

### Grandma / baseline / low / repetition 2

- **Find overdue invoices:** Checks invoice records and lists unpaid invoices whose due dates are already past.
- **Inspect invoice checker:** Displays the first part of a local invoice-checking script to verify logic.
- **Change waiting limit:** Shortens request waiting time from 45 seconds to 15 seconds.
- **Unknown task:** Starts a custom automation step; its exact purpose is not shown.

### Grandma / refined / low / repetition 2

- **Find overdue invoices:** Run the invoice checker to print unpaid past-due invoice number, customer, and amount.
- **Inspect invoice checker:** Read the invoice checker source to see it compares unpaid flags and due dates.
- **Change waiting limit:** Change network/client.ts: reduce request waiting from 45,000 to 15,000 milliseconds.
- **Unknown task:** Start a task whose purpose is not yet clear.

### Grandma / refined / medium / repetition 2

- **Find overdue invoices:** Run overdue_invoices.py to list invoices still unpaid and with due dates before today.
- **Inspect invoice checker:** Read first section of overdue_invoices.py to see how overdue unpaid invoices are selected.
- **Change waiting limit:** Change network/client.ts to reduce the waiting time from 45 to 15 seconds.
- **Unknown task:** Run task_zeta.sh; its purpose is not yet clear from supplied evidence.
