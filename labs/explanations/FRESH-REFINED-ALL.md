# Fresh live experiment: CURRENT vs REFINED at every level

Spark low; four new synthetic activities; three repetitions per condition. 24 fresh calls and 96 labels. No production deployment. Developer bypass verified without model calls. Current is the unchanged shipping prompt plus audience policy; refined adds level-specific examples and wording rules.

Tables show repetition zero consistently, with all repetitions in the appendix.

## Findings

Current won on simplicity in several cases. Refined Technical retained useful
paths and exact filtering conditions, but refined Balanced often looked like
Technical, and refined Plain English was noticeably more verbose. Both versions
kept the difference between running and reading the invoice checker, and both
acknowledged the unknown task's purpose throughout this fresh set.

23/24 batches passed the lab's format/160-character checks. Refined Balanced's
third repetition exceeded 160 characters on the inspection label; its actual
answer is retained, not rerun or excluded from timing. Timing includes all trials.

Refined did not show a speed improvement: Plain English was slower in every
paired repetition, and Grandma was slower in two of three. Small samples do not
establish a production SLA. Neither version consistently avoids technical wording
at Grandma. There is no justification here for replacing all current prompts.

## Measured batch times

| Level | Current median | Refined median |
|---|---:|---:|
| Developer | No model | No model |
| Technical | 3.47 s | 3.52 s |
| Balanced | 3.37 s | 3.37 s |
| Plain English | 3.47 s | 5.28 s |
| Grandma | 3.42 s | 3.97 s |

## Find overdue invoices

| Level | CURRENT | REFINED |
|---|---|---|
| Developer | `python overdue_invoices.py` | Same raw activity |
| Technical | Runs a Python utility that loads invoices.csv and prints invoice_number, customer, and amount for unpaid rows whose due_date is before today. | Run overdue_invoices.py: reads invoices.csv and prints invoice_number, customer, and amount when paid is 'false' and due_date is before today. |
| Balanced | Runs an invoice-check process that scans invoice records and prints only unpaid entries with due dates earlier than today. | Run overdue_invoices.py to print invoice_number, customer, and amount for invoices marked unpaid with due_date before today. |
| Plain English | Executes a check that lists unpaid invoices whose due dates are past today. | Run a script that checks invoice records and prints invoice number, customer, and amount for unpaid invoices past their due date. |
| Grandma | Reads invoice records and outputs each unpaid, past-due invoice’s id, customer, and amount. | Run unpaid-invoice check; list number, customer and amount for overdue bills. |

## Inspect invoice checker

| Level | CURRENT | REFINED |
|---|---|---|
| Developer | `sed -n '1,80p' overdue_invoices.py` | Same raw activity |
| Technical | Displays the first 80 lines of overdue_invoices.py to inspect its implementation and verify invoice-overdue filtering logic. | Read overdue_invoices.py with sed to inspect a script that parses invoices.csv and selects rows with paid='false' and due_date earlier than today. |
| Balanced | Displays the beginning of the invoice script to inspect the logic used for reading CSV data and selecting overdue unpaid invoices. | Inspect overdue_invoices.py source to read its logic: it reads invoices.csv and prints fields for rows where paid is false and due_date is earlier than today. |
| Plain English | Displays the beginning of a script to inspect how it works without making changes. | Read the code used to find overdue unpaid invoices and confirm it filters by paid flag and due date before listing invoice details. |
| Grandma | Checks the first part of that invoice script to verify date and payment checks. | Read the source code to confirm how the overdue-invoice check works. |

## Change waiting limit

| Level | CURRENT | REFINED |
|---|---|---|
| Developer | `Change request timeout from 45000 ms to 15000 ms.` | Same raw activity |
| Technical | Updates the network client configuration to reduce the request timeout setting from 45,000 ms to 15,000 ms. | Edit network/client.ts to change request timeout from 45000 ms to 15000 ms. |
| Balanced | Reduces a client network request timeout setting from 45,000 ms to 15,000 ms. | Edit network/client.ts to change request timeout setting from 45,000 ms to 15,000 ms. |
| Plain English | Edits a client network setting to shorten the request timeout from 45s to 15s. | Change the network request waiting limit from 45 seconds to 15 seconds. |
| Grandma | Cuts the request wait time from 45 seconds down to 15 seconds. | Reduce the network wait period from 45 seconds to 15 seconds. |

## Unknown task

| Level | CURRENT | REFINED |
|---|---|---|
| Developer | `bash task_zeta.sh` | Same raw activity |
| Technical | Starts execution of task_zeta.sh via Bash; the specific task performed is not shown in the provided data. | Run task_zeta.sh; its purpose is unknown from the available evidence. |
| Balanced | Executes an external task script named task_zeta, but its internal purpose is not inferable from the supplied activity data. | Run task_zeta.sh; its purpose is unknown from the provided evidence. |
| Plain English | Runs a local automated task script; the exact action cannot be determined from the log. | Run a shell workflow with no supplied details; its purpose is unknown from the provided evidence. |
| Grandma | Runs a task script, but its exact action is not shown here. | Start a task whose purpose is not yet clear. |

## All repetitions

### Technical, baseline, repetition 0

- **Find overdue invoices:** Runs a Python utility that loads invoices.csv and prints invoice_number, customer, and amount for unpaid rows whose due_date is before today.
- **Inspect invoice checker:** Displays the first 80 lines of overdue_invoices.py to inspect its implementation and verify invoice-overdue filtering logic.
- **Change waiting limit:** Updates the network client configuration to reduce the request timeout setting from 45,000 ms to 15,000 ms.
- **Unknown task:** Starts execution of task_zeta.sh via Bash; the specific task performed is not shown in the provided data.

### Technical, refined, repetition 0

- **Find overdue invoices:** Run overdue_invoices.py: reads invoices.csv and prints invoice_number, customer, and amount when paid is 'false' and due_date is before today.
- **Inspect invoice checker:** Read overdue_invoices.py with sed to inspect a script that parses invoices.csv and selects rows with paid='false' and due_date earlier than today.
- **Change waiting limit:** Edit network/client.ts to change request timeout from 45000 ms to 15000 ms.
- **Unknown task:** Run task_zeta.sh; its purpose is unknown from the available evidence.

### Balanced, refined, repetition 0

- **Find overdue invoices:** Run overdue_invoices.py to print invoice_number, customer, and amount for invoices marked unpaid with due_date before today.
- **Inspect invoice checker:** Inspect overdue_invoices.py source to read its logic: it reads invoices.csv and prints fields for rows where paid is false and due_date is earlier than today.
- **Change waiting limit:** Edit network/client.ts to change request timeout setting from 45,000 ms to 15,000 ms.
- **Unknown task:** Run task_zeta.sh; its purpose is unknown from the provided evidence.

### Balanced, baseline, repetition 0

- **Find overdue invoices:** Runs an invoice-check process that scans invoice records and prints only unpaid entries with due dates earlier than today.
- **Inspect invoice checker:** Displays the beginning of the invoice script to inspect the logic used for reading CSV data and selecting overdue unpaid invoices.
- **Change waiting limit:** Reduces a client network request timeout setting from 45,000 ms to 15,000 ms.
- **Unknown task:** Executes an external task script named task_zeta, but its internal purpose is not inferable from the supplied activity data.

### Plain English, baseline, repetition 0

- **Find overdue invoices:** Executes a check that lists unpaid invoices whose due dates are past today.
- **Inspect invoice checker:** Displays the beginning of a script to inspect how it works without making changes.
- **Change waiting limit:** Edits a client network setting to shorten the request timeout from 45s to 15s.
- **Unknown task:** Runs a local automated task script; the exact action cannot be determined from the log.

### Plain English, refined, repetition 0

- **Find overdue invoices:** Run a script that checks invoice records and prints invoice number, customer, and amount for unpaid invoices past their due date.
- **Inspect invoice checker:** Read the code used to find overdue unpaid invoices and confirm it filters by paid flag and due date before listing invoice details.
- **Change waiting limit:** Change the network request waiting limit from 45 seconds to 15 seconds.
- **Unknown task:** Run a shell workflow with no supplied details; its purpose is unknown from the provided evidence.

### Grandma, refined, repetition 0

- **Find overdue invoices:** Run unpaid-invoice check; list number, customer and amount for overdue bills.
- **Inspect invoice checker:** Read the source code to confirm how the overdue-invoice check works.
- **Change waiting limit:** Reduce the network wait period from 45 seconds to 15 seconds.
- **Unknown task:** Start a task whose purpose is not yet clear.

### Grandma, baseline, repetition 0

- **Find overdue invoices:** Reads invoice records and outputs each unpaid, past-due invoice’s id, customer, and amount.
- **Inspect invoice checker:** Checks the first part of that invoice script to verify date and payment checks.
- **Change waiting limit:** Cuts the request wait time from 45 seconds down to 15 seconds.
- **Unknown task:** Runs a task script, but its exact action is not shown here.

### Technical, refined, repetition 1

- **Find overdue invoices:** Run overdue_invoices.py; it reads invoices.csv and prints invoice_number, customer, and amount for rows where paid is false and due_date is before today.
- **Inspect invoice checker:** Read overdue_invoices.py with sed -n '1,80p' to inspect the filter that prints unpaid, past-due invoices (invoice_number, customer, amount).
- **Change waiting limit:** Edit network/client.ts to change the request timeout from 45000 ms to 15000 ms.
- **Unknown task:** Run bash task_zeta.sh; its purpose is unknown from the available evidence.

### Technical, baseline, repetition 1

- **Find overdue invoices:** Reads invoice records from a CSV and outputs invoice number, customer, and amount for unpaid entries whose due date is earlier than today.
- **Inspect invoice checker:** Views the top part of the invoice-processing code to inspect how overdue unpaid-invoice filtering is implemented.
- **Change waiting limit:** Edits the network client timeout setting, reducing request timeout from 45,000 ms to 15,000 ms.
- **Unknown task:** Executes a local automation shell script; its exact purpose is not provided in the available evidence.

### Balanced, baseline, repetition 1

- **Find overdue invoices:** Runs a local overdue-invoice check that reads invoice records, filters unpaid entries past their due date, and outputs key invoice details.
- **Inspect invoice checker:** Reads the first part of a Python script to inspect how it determines and outputs overdue unpaid invoices.
- **Change waiting limit:** Edits a network client configuration to reduce the request timeout from 45,000 ms to 15,000 ms.
- **Unknown task:** Executes an automated shell task sequence; the specific operation is not shown in the provided log.

### Balanced, refined, repetition 1

- **Find overdue invoices:** Run overdue_invoices.py to read invoices.csv and print invoice number, customer, and amount for rows marked unpaid with past-due dates.
- **Inspect invoice checker:** Inspect overdue_invoices.py source to review its unpaid/overdue filtering and print behavior; no execution is performed.
- **Change waiting limit:** Edit network/client.ts to reduce the request timeout setting from 45000 ms to 15000 ms.
- **Unknown task:** Run task_zeta.sh; script purpose is unknown from provided evidence.

### Plain English, refined, repetition 1

- **Find overdue invoices:** Run a script that reads invoice records and outputs invoice number, customer, and amount for unpaid items with a due date before today.
- **Inspect invoice checker:** Read the script’s source code to check how it identifies unpaid invoices and compares due dates.
- **Change waiting limit:** Change the waiting-for-reply time from 45,000 ms to 15,000 ms in a client setting.
- **Unknown task:** Run a script whose purpose is unknown because the script contents were not provided.

### Plain English, baseline, repetition 1

- **Find overdue invoices:** Runs a local report that scans invoice records and prints any unpaid invoices with past due dates.
- **Inspect invoice checker:** Views the first part of a billing script to confirm it reads invoice CSV rows and checks unpaid-overdue logic.
- **Change waiting limit:** Edits a network request setting to reduce the timeout from 45,000 ms to 15,000 ms.
- **Unknown task:** Runs a local task script; its exact action is not clear from the provided details.

### Grandma, baseline, repetition 1

- **Find overdue invoices:** Checks invoice data and prints unpaid entries with past-due dates.
- **Inspect invoice checker:** Shows the first part of a program so its invoice-check logic can be reviewed.
- **Change waiting limit:** Reduces the request timeout from 45 seconds to 15 seconds.
- **Unknown task:** Runs a local task command; the exact action is not visible here.

### Grandma, refined, repetition 1

- **Find overdue invoices:** Run invoice checker to print invoice number, customer, and amount for unpaid overdue bills.
- **Inspect invoice checker:** Read the invoice checker source to see how overdue unpaid bills are selected.
- **Change waiting limit:** Lower the wait limit for network replies from 45 seconds to 15 seconds.
- **Unknown task:** Run a stored shell task; its purpose is not yet clear.

### Technical, baseline, repetition 2

- **Find overdue invoices:** Runs a Python-based invoice check that reads invoice records and prints customers’ invoice IDs and amounts that are unpaid and past their due date.
- **Inspect invoice checker:** Reads and displays the first 80 lines of a script file to inspect its current contents.
- **Change waiting limit:** Updates the network client’s request timeout setting from 45,000 ms down to 15,000 ms.
- **Unknown task:** Runs a shell task script; exact behavior is not shown in the provided data, so its specific action is unknown.

### Technical, refined, repetition 2

- **Find overdue invoices:** Run overdue_invoices.py to read invoices.csv and print invoice_number, customer, and amount for rows where paid is false and due_date is before today.
- **Inspect invoice checker:** Read overdue_invoices.py with sed -n '1,80p' to inspect logic that loads CSV rows and filters unpaid invoices with past due_date.
- **Change waiting limit:** Edit network/client.ts, changing the request timeout from 45000 ms to 15000 ms.
- **Unknown task:** Run bash task_zeta.sh; its purpose is unknown from the available evidence.

### Balanced, refined, repetition 2

- **Find overdue invoices:** Run overdue_invoices.py to read invoices.csv, filter unpaid records with past due dates, and print each matching invoice number, customer, and amount.
- **Inspect invoice checker:** Inspect overdue_invoices.py by printing its first lines, showing logic that checks unpaid rows against today’s date before printing invoice number, customer, and amount.
- **Change waiting limit:** Edit network/client.ts to change the request timeout setting from 45,000 ms to 15,000 ms.
- **Unknown task:** Run task_zeta.sh; the script’s purpose is not established from the provided evidence, so its purpose is unknown.

### Balanced, baseline, repetition 2

- **Find overdue invoices:** Runs a local process to read invoice records and print entries marked unpaid whose due dates are already past.
- **Inspect invoice checker:** Reads a file’s opening lines to inspect logic that selects unpaid invoices and checks due-date expiration.
- **Change waiting limit:** Changes a network-client timeout configuration from 45,000 ms to 15,000 ms to reduce wait time on slow requests.
- **Unknown task:** Runs a local shell task helper script; its exact purpose is not specified in the provided data.

### Plain English, baseline, repetition 2

- **Find overdue invoices:** Runs a local check to list unpaid invoices that are past their due date.
- **Inspect invoice checker:** Reads the top section of a script to review its existing logic without editing it.
- **Change waiting limit:** Updates a network client setting so request timeouts are reduced from 45 seconds to 15 seconds.
- **Unknown task:** Runs an automation task script; its exact purpose is not provided in the available data.

### Plain English, refined, repetition 2

- **Find overdue invoices:** Runs a script that reads invoice data and prints invoice number, customer, and amount for unpaid rows whose due date is before today.
- **Inspect invoice checker:** Reads the invoice-check script to inspect its logic for selecting unpaid invoices whose due dates are earlier than today.
- **Change waiting limit:** Reduces request waiting time from 45,000 ms to 15,000 ms.
- **Unknown task:** Runs a shell task script; its purpose is unknown from the provided evidence.

### Grandma, refined, repetition 2

- **Find overdue invoices:** Run the overdue invoice checker to list unpaid, past-due invoice number, customer, and amount.
- **Inspect invoice checker:** Read the source to learn how it identifies unpaid invoices with due dates before today.
- **Change waiting limit:** Lower the service wait time from 45 seconds to 15 seconds in network requests.
- **Unknown task:** Start a task whose purpose is not yet clear.

### Grandma, baseline, repetition 2

- **Find overdue invoices:** Runs a check that lists unpaid invoices whose due dates are in the past.
- **Inspect invoice checker:** Reads the top of the invoice checker file to verify its logic.
- **Change waiting limit:** Edits a request setting to shorten timeout from 45 seconds to 15 seconds.
- **Unknown task:** Runs a shell task, but its exact purpose is not shown.
