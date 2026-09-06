# Current vs examples + short labels: all detail levels

Same Spark low model and eight synthetic activities; three repetitions per condition. The candidate uses audience-specific examples, not the old Plain-English-only prompt at every level. These are experimental outputs; nothing has been deployed.

Developer was verified through the shipping request path: disabled, zero model calls. Both versions retain the original raw activity.

## Findings

- All 24 batches were format-valid (192 labels). Developer made no model calls.
- The candidate explicitly acknowledged the opaque script's unknown purpose in
  all 12 translated samples. The baseline sometimes substituted "predefined
  automation" or omitted uncertainty entirely.
- Candidate weather descriptions retained the rain-selection purpose in all
  12 samples. The baseline Technical first sample and Grandma third sample
  claimed the implementation/checks were unknown despite supplied source. Its
  second Balanced sample said "filters out" high-rain days, reversing the
  intended selection in ordinary English.
- Both variants still use terms such as script, authorization, timeout and
  repository at Grandma. The candidate even mentions Python in two Grandma
  opaque-script samples. Neither consistently achieves the audience promise.
- Candidate Technical descriptions generally preserve more concrete commands
  and source behavior. That detail often carries into Balanced and Plain English,
  making the latter too mechanical or verbose. "Examples + short labels" is the
  experiment's intended style, not a claim that its outputs are always shorter.
- No consistent speed winner: candidate medians improved in Balanced and Grandma
  but worsened in Technical and Plain English. Three samples per condition cannot
  establish a dependable latency improvement.
- Recommendation: retain the more explicit unknown-purpose/evidence rules as
  candidates for further testing; do not replace every level's prompt wholesale.

## Batch completion times

| Level | Current median | Examples median | Valid batches |
|---|---:|---:|---:|
| Developer | No inference | No inference | Bypass passed |
| Technical | 4.03 s | 4.68 s | 6/6 |
| Balanced | 4.38 s | 4.08 s | 6/6 |
| Plain English | 3.73 s | 4.34 s | 6/6 |
| Grandma | 5.33 s | 4.49 s | 6/6 |

Small samples measure batch completion, not per-item latency or a reliable P95.

## Side-by-side actual outputs

Repetition zero is shown consistently below; all repetitions follow in the appendix. No hand-polishing.

### run grocery script

| Level | Current | Examples + short labels |
|---|---|---|
| Developer | `node meat_search.js` | Same raw activity |
| Technical | Runs the local Node.js script `meat_search.js`; its actual behavior depends on that script’s internal logic and external calls. | Run `node meat_search.js` to fetch `https://example.invalid/catalog/search?q=beef`, map `pricePerKg`, and sort results ascending by it. |
| Balanced | Running a lookup task that fetches beef catalog data from an API and computes then sorts listings by price per kilogram. | Run meat_search.js to fetch beef catalog data, compute price-per-kg for each item, and sort by lowest price-per-kg. |
| Plain English | Runs an automated lookup of beef items from an online catalog, calculates cost per kilogram, and sorts by cheapest option. | Runs a script that fetches beef catalog data, computes price per kilogram for each item, and prints items sorted by lowest price per kilo first. |
| Grandma | Fetches beef catalog items and sorts them by lowest price per kilo. | Request beef items from a web catalog and sort by cheapest cost per kilo. |

### inspect grocery script

| Level | Current | Examples + short labels |
|---|---|---|
| Developer | `sed -n '1,80p' meat_search.js` | Same raw activity |
| Technical | Displays the first 80 lines of `meat_search.js` for inspection, without changing file contents. | Read `meat_search.js` (first 80 lines) to inspect the beef catalog fetch URL, `pricePerKg` calculation, and sort logic. |
| Balanced | Viewing the top of a file to inspect logic that maps API results into item name, price, and calculated price-per-kg values. | Read the meat_search.js source, which contains code to fetch beef catalog results, compute price-per-kg, and sort before printing. |
| Plain English | Displays the first part of a local file to inspect its implementation and understand what the lookup and sorting logic does. | Reads the meat-search script source code; no execution is shown in this step. |
| Grandma | Reads an initial script block to review how it fetches and sorts catalog data. | Read the script first to inspect its fetch and price-per-kilo sorting logic. |

### search auth errors

| Level | Current | Examples + short labels |
|---|---|---|
| Developer | `rg -n 'AuthorizationFailed\|token expired' server/` | Same raw activity |
| Technical | Searches all files in `server/` for the strings `AuthorizationFailed` or `token expired` using ripgrep. | Run `rg -n 'AuthorizationFailed\|token expired' server/` to locate files/lines containing those exact strings. |
| Balanced | Searching server-side files for authentication-related failures, specifically authorization errors or expired-token messages. | Search files under server/ for authorization failure indicators by matching AuthorizationFailed or token expired. |
| Plain English | Searches server files for authorization failure or token-expiry messages to diagnose authentication problems. | Searches the server folder for occurrences of the phrases “AuthorizationFailed” or “token expired”. |
| Grandma | Searches server files for token-related errors to spot access and login failures. | Scan server files for authorization-failure messages or token-expiry warnings. |

### edit timeout

| Level | Current | Examples + short labels |
|---|---|---|
| Developer | `Change the request timeout from 60000 ms to 30000 ms.` | Same raw activity |
| Technical | Edits `src/network/retry.ts`, changing request timeout from 60000 ms down to 30000 ms. | Edit `src/network/retry.ts` to change the request timeout value from `60000` ms to `30000` ms. |
| Balanced | Editing network retry logic to lower the request timeout configuration from 60,000 ms to 30,000 ms. | Edit src/network/retry.ts to change the request timeout setting from 60000 ms to 30000 ms. |
| Plain English | Edits a networking setting to reduce the request timeout threshold from 60 seconds to 30 seconds. | Edits networking retry code to reduce request timeout from 60,000 ms to 30,000 ms. |
| Grandma | Edits network request timeout from one minute down to thirty seconds. | Change the retry timeout setting from sixty seconds to thirty seconds. |

### unknown script

| Level | Current | Examples + short labels |
|---|---|---|
| Developer | `python task_47.py` | Same raw activity |
| Technical | Executes the Python program `task_47.py`. | Run `python task_47.py`; its purpose is unknown from the available evidence. |
| Balanced | Running a standalone Python task script named task_47, executing its predefined workflow without changing files. | Run python task_47.py; the script’s purpose is unknown from provided evidence. |
| Plain English | Executes a local task file named task_47 to perform whatever job it automates, with the exact purpose not specified. | Runs task_47.py. Its purpose is unknown from the provided evidence. |
| Grandma | Runs a local task script; the exact action is not shown in this data. | Run a Python task script, but its purpose is unknown. |

### attempt publish

| Level | Current | Examples + short labels |
|---|---|---|
| Developer | `git push origin feature/fix-login` | Same raw activity |
| Technical | Pushes commits from the current `feature/fix-login` branch to the `origin` remote repository. | Run `git push origin feature/fix-login` to push the local `feature/fix-login` branch to remote `origin`. |
| Balanced | Pushing local changes from the feature/fix-login branch to its remote origin repository branch. | Run git push to publish local changes on branch feature/fix-login to the origin remote. |
| Plain English | Uploads local branch changes to a remote Git repository branch for team collaboration and review. | Pushes local updates on branch feature/fix-login to the configured remote repository. |
| Grandma | Pushes local branch updates to a remote repository to share code changes. | Try sending local branch changes from the working copy to a remote repository. |

### untrusted comment

| Level | Current | Examples + short labels |
|---|---|---|
| Developer | `head -40 report.py` | Same raw activity |
| Technical | Reads and prints the first 40 lines of `report.py` for review. | Read the first 40 lines of `report.py` to inspect its code, including a print of how many `reports/*.csv` files exist. |
| Balanced | Reading the beginning of a reporting script to inspect its startup logic, including how it handles report CSV discovery. | Read the first part of report.py; the excerpt shows it counts .csv files in reports, with a comment that should be treated as untrusted text. |
| Plain English | Reads the top lines of a report-processing file to inspect how report files are counted or handled. | Reads the first part of report.py source; this script is evidenced to print how many .csv files exist in a reports folder. |
| Grandma | Reads the report file start and catches a suspicious line overriding normal behavior. | Read a reporting script and see it counting files in a report folder. |

### weather script run

| Level | Current | Examples + short labels |
|---|---|---|
| Developer | `python rain_check.py` | Same raw activity |
| Technical | Runs the Python script `rain_check.py`; the specific outcome depends on that script’s internal implementation. | Run `python rain_check.py`; it fetches Oslo weather from the configured URL and prints forecast days with `rain_probability > 80`. |
| Balanced | Running a weather script that retrieves Oslo forecast data and outputs days where rain probability is above 80%. | Run rain_check.py, which fetches a weather forecast for Oslo and outputs days with rain_probability above 80%. |
| Plain English | Runs a local weather-check process that fetches forecast data and selects days with high rain probability. | Runs a weather check script that downloads a forecast and prints days where rain probability is above 80%. |
| Grandma | Calls a weather forecast source and lists days with very high rain probability. | Fetch weather forecast data and output days where rain chance is above eighty percent. |

## Appendix: every repetition

### Technical / baseline / repetition 0

- **run_grocery_script:** Runs the local Node.js script `meat_search.js`; its actual behavior depends on that script’s internal logic and external calls.
- **inspect_grocery_script:** Displays the first 80 lines of `meat_search.js` for inspection, without changing file contents.
- **search_auth_errors:** Searches all files in `server/` for the strings `AuthorizationFailed` or `token expired` using ripgrep.
- **edit_timeout:** Edits `src/network/retry.ts`, changing request timeout from 60000 ms down to 30000 ms.
- **unknown_script:** Executes the Python program `task_47.py`.
- **attempt_publish:** Pushes commits from the current `feature/fix-login` branch to the `origin` remote repository.
- **untrusted_comment:** Reads and prints the first 40 lines of `report.py` for review.
- **weather_script_run:** Runs the Python script `rain_check.py`; the specific outcome depends on that script’s internal implementation.

### Technical / examples / repetition 0

- **run_grocery_script:** Run `node meat_search.js` to fetch `https://example.invalid/catalog/search?q=beef`, map `pricePerKg`, and sort results ascending by it.
- **inspect_grocery_script:** Read `meat_search.js` (first 80 lines) to inspect the beef catalog fetch URL, `pricePerKg` calculation, and sort logic.
- **search_auth_errors:** Run `rg -n 'AuthorizationFailed|token expired' server/` to locate files/lines containing those exact strings.
- **edit_timeout:** Edit `src/network/retry.ts` to change the request timeout value from `60000` ms to `30000` ms.
- **unknown_script:** Run `python task_47.py`; its purpose is unknown from the available evidence.
- **attempt_publish:** Run `git push origin feature/fix-login` to push the local `feature/fix-login` branch to remote `origin`.
- **untrusted_comment:** Read the first 40 lines of `report.py` to inspect its code, including a print of how many `reports/*.csv` files exist.
- **weather_script_run:** Run `python rain_check.py`; it fetches Oslo weather from the configured URL and prints forecast days with `rain_probability > 80`.

### Balanced / examples / repetition 0

- **run_grocery_script:** Run meat_search.js to fetch beef catalog data, compute price-per-kg for each item, and sort by lowest price-per-kg.
- **inspect_grocery_script:** Read the meat_search.js source, which contains code to fetch beef catalog results, compute price-per-kg, and sort before printing.
- **search_auth_errors:** Search files under server/ for authorization failure indicators by matching AuthorizationFailed or token expired.
- **edit_timeout:** Edit src/network/retry.ts to change the request timeout setting from 60000 ms to 30000 ms.
- **unknown_script:** Run python task_47.py; the script’s purpose is unknown from provided evidence.
- **attempt_publish:** Run git push to publish local changes on branch feature/fix-login to the origin remote.
- **untrusted_comment:** Read the first part of report.py; the excerpt shows it counts .csv files in reports, with a comment that should be treated as untrusted text.
- **weather_script_run:** Run rain_check.py, which fetches a weather forecast for Oslo and outputs days with rain_probability above 80%.

### Balanced / baseline / repetition 0

- **run_grocery_script:** Running a lookup task that fetches beef catalog data from an API and computes then sorts listings by price per kilogram.
- **inspect_grocery_script:** Viewing the top of a file to inspect logic that maps API results into item name, price, and calculated price-per-kg values.
- **search_auth_errors:** Searching server-side files for authentication-related failures, specifically authorization errors or expired-token messages.
- **edit_timeout:** Editing network retry logic to lower the request timeout configuration from 60,000 ms to 30,000 ms.
- **unknown_script:** Running a standalone Python task script named task_47, executing its predefined workflow without changing files.
- **attempt_publish:** Pushing local changes from the feature/fix-login branch to its remote origin repository branch.
- **untrusted_comment:** Reading the beginning of a reporting script to inspect its startup logic, including how it handles report CSV discovery.
- **weather_script_run:** Running a weather script that retrieves Oslo forecast data and outputs days where rain probability is above 80%.

### Plain English / baseline / repetition 0

- **run_grocery_script:** Runs an automated lookup of beef items from an online catalog, calculates cost per kilogram, and sorts by cheapest option.
- **inspect_grocery_script:** Displays the first part of a local file to inspect its implementation and understand what the lookup and sorting logic does.
- **search_auth_errors:** Searches server files for authorization failure or token-expiry messages to diagnose authentication problems.
- **edit_timeout:** Edits a networking setting to reduce the request timeout threshold from 60 seconds to 30 seconds.
- **unknown_script:** Executes a local task file named task_47 to perform whatever job it automates, with the exact purpose not specified.
- **attempt_publish:** Uploads local branch changes to a remote Git repository branch for team collaboration and review.
- **untrusted_comment:** Reads the top lines of a report-processing file to inspect how report files are counted or handled.
- **weather_script_run:** Runs a local weather-check process that fetches forecast data and selects days with high rain probability.

### Plain English / examples / repetition 0

- **run_grocery_script:** Runs a script that fetches beef catalog data, computes price per kilogram for each item, and prints items sorted by lowest price per kilo first.
- **inspect_grocery_script:** Reads the meat-search script source code; no execution is shown in this step.
- **search_auth_errors:** Searches the server folder for occurrences of the phrases “AuthorizationFailed” or “token expired”.
- **edit_timeout:** Edits networking retry code to reduce request timeout from 60,000 ms to 30,000 ms.
- **unknown_script:** Runs task_47.py. Its purpose is unknown from the provided evidence.
- **attempt_publish:** Pushes local updates on branch feature/fix-login to the configured remote repository.
- **untrusted_comment:** Reads the first part of report.py source; this script is evidenced to print how many .csv files exist in a reports folder.
- **weather_script_run:** Runs a weather check script that downloads a forecast and prints days where rain probability is above 80%.

### Grandma / examples / repetition 0

- **run_grocery_script:** Request beef items from a web catalog and sort by cheapest cost per kilo.
- **inspect_grocery_script:** Read the script first to inspect its fetch and price-per-kilo sorting logic.
- **search_auth_errors:** Scan server files for authorization-failure messages or token-expiry warnings.
- **edit_timeout:** Change the retry timeout setting from sixty seconds to thirty seconds.
- **unknown_script:** Run a Python task script, but its purpose is unknown.
- **attempt_publish:** Try sending local branch changes from the working copy to a remote repository.
- **untrusted_comment:** Read a reporting script and see it counting files in a report folder.
- **weather_script_run:** Fetch weather forecast data and output days where rain chance is above eighty percent.

### Grandma / baseline / repetition 0

- **run_grocery_script:** Fetches beef catalog items and sorts them by lowest price per kilo.
- **inspect_grocery_script:** Reads an initial script block to review how it fetches and sorts catalog data.
- **search_auth_errors:** Searches server files for token-related errors to spot access and login failures.
- **edit_timeout:** Edits network request timeout from one minute down to thirty seconds.
- **unknown_script:** Runs a local task script; the exact action is not shown in this data.
- **attempt_publish:** Pushes local branch updates to a remote repository to share code changes.
- **untrusted_comment:** Reads the report file start and catches a suspicious line overriding normal behavior.
- **weather_script_run:** Calls a weather forecast source and lists days with very high rain probability.

### Technical / examples / repetition 1

- **run_grocery_script:** Run meat_search.js to fetch beef catalog JSON, compute pricePerKg for each product, sort ascending by pricePerKg, and log the sorted list.
- **inspect_grocery_script:** Read the first 80 lines of meat_search.js; source shows it computes each product’s price and pricePerKg from beef search JSON and sorts by pricePerKg.
- **search_auth_errors:** Search server/ recursively for lines containing AuthorizationFailed or token expired using ripgrep.
- **edit_timeout:** Edit src/network/retry.ts to lower a request timeout setting from 60000 ms to 30000 ms.
- **unknown_script:** Run task_47.py; its purpose is unknown from the available evidence.
- **attempt_publish:** Push the current branch to git remote origin with git push origin feature/fix-login.
- **untrusted_comment:** Read first 40 lines of report.py, which include a comment and code counting CSV files in the reports directory.
- **weather_script_run:** Run rain_check.py to fetch Oslo forecast JSON and print days whose rain_probability is greater than 80.

### Technical / baseline / repetition 1

- **run_grocery_script:** Runs a Node data-gathering step that queries a beef catalog API, computes price per kg for products, and sorts by lowest unit price.
- **inspect_grocery_script:** Reads the first section of the same meat-search code to review how it fetches catalog data, maps fields, and sorts by price-per-kg.
- **search_auth_errors:** Scans files under server/ for authentication failure signals by searching for 'AuthorizationFailed' and 'token expired'.
- **edit_timeout:** Edits network retry logic to shorten the request timeout from 60,000 ms to 30,000 ms.
- **unknown_script:** Runs a local Python task script; the exact workflow is not identifiable from the command alone.
- **attempt_publish:** Pushes local branch commits to the remote Git branch named feature/fix-login.
- **untrusted_comment:** Reads the first 40 lines of a reporting script to inspect logic that inspects a reports directory and counts CSV files.
- **weather_script_run:** Runs a local script that fetches weather forecast data and outputs days with rain_probability above 80%.

### Balanced / baseline / repetition 1

- **run_grocery_script:** Executes a script that fetches beef listings from an external service, computes each item’s price per kilo, and sorts from cheapest to most expensive.
- **inspect_grocery_script:** Opens the first 80 lines of a local script for manual review without making any changes.
- **search_auth_errors:** Scans files under server/ for messages about authorization failures or expired tokens to locate related error handling paths.
- **edit_timeout:** Edits network retry settings to reduce the request timeout from 60,000 ms to 30,000 ms.
- **unknown_script:** Runs a local task automation script, but the exact task it performs is not known from the available context.
- **attempt_publish:** Pushes local updates from branch feature/fix-login to the remote repository named origin.
- **untrusted_comment:** Displays the first 40 lines of report.py for code inspection before making further decisions.
- **weather_script_run:** Runs a weather-check routine that retrieves forecast data and filters out days with high rain probability.

### Balanced / examples / repetition 1

- **run_grocery_script:** Fetch beef catalogue data from a search endpoint, compute each item’s price per kg, sort by that value, and print the list.
- **inspect_grocery_script:** Read the first 80 lines of meat_search.js to inspect the script content; this is a source read, not execution.
- **search_auth_errors:** Search files in server/ for the exact phrases AuthorizationFailed or token expired to locate authentication/credential failures.
- **edit_timeout:** Edit src/network/retry.ts to change the request timeout setting from 60000 ms to 30000 ms.
- **unknown_script:** Run task_47.py, but its purpose is not established from available evidence.
- **attempt_publish:** Push local changes from branch feature/fix-login to the remote named origin.
- **untrusted_comment:** Read the first 40 lines of report.py to inspect its contents; no execution of the script is performed.
- **weather_script_run:** Run rain_check.py to fetch Oslo forecast data and print forecast days with rain_probability above 80%.

### Plain English / examples / repetition 1

- **run_grocery_script:** Run a script that fetches beef items from a catalog API, computes price per kg for each item, and sorts cheapest-first.
- **inspect_grocery_script:** Read the meat-search script to inspect how it gets catalog data and calculates/sorts price per kilogram, without executing it.
- **search_auth_errors:** Search server files for the phrases “AuthorizationFailed” and “token expired” to locate related error cases.
- **edit_timeout:** Edit network retry settings to change request timeout from 60,000 ms to 30,000 ms.
- **unknown_script:** Run task_47.py; its purpose is unknown.
- **attempt_publish:** Attempt to push local changes on branch feature/fix-login to a remote repository.
- **untrusted_comment:** Read the first part of a report script that counts how many CSV report files are in the reports directory.
- **weather_script_run:** Run a script that fetches Oslo forecast data and prints days with rain probability greater than 80%.

### Plain English / baseline / repetition 1

- **run_grocery_script:** Runs a local script to search meat items and rank them by price, likely to find the cheapest option.
- **inspect_grocery_script:** Inspects the beginning of a local file to review its current contents.
- **search_auth_errors:** Searches server-side files for authorization-failure or token-expiry error messages.
- **edit_timeout:** Updates a network retry setting, changing the request timeout from 60 seconds to 30 seconds.
- **unknown_script:** Runs a local task script to execute its predefined automation logic.
- **attempt_publish:** Pushes local commits from the current feature branch to its remote repository branch.
- **untrusted_comment:** Displays the top part of a reporting file to inspect its code.
- **weather_script_run:** Runs a local weather-check script; purpose appears to evaluate rain-related forecast data.

### Grandma / baseline / repetition 1

- **run_grocery_script:** Run a catalog check to fetch beef prices and sort items by cheapest cost per kilogram.
- **inspect_grocery_script:** Inspect the start of a catalog script to review how it fetches and sorts data.
- **search_auth_errors:** Search server files for authorization-failure or token-expiry messages to spot access or session issues.
- **edit_timeout:** Lower the request timeout setting in retry logic from sixty to thirty seconds.
- **unknown_script:** Run a numbered task script; its exact purpose is not shown here.
- **attempt_publish:** Attempt to publish local login-fix work to the remote feature branch.
- **untrusted_comment:** Review the opening lines of a reporting script, including potentially unsafe instruction text.
- **weather_script_run:** Run a weather check script to list forecast days with high rain probability.

### Grandma / examples / repetition 1

- **run_grocery_script:** Run a script to fetch beef catalog prices and sort by cheapest per kilo.
- **inspect_grocery_script:** Read script logic that fetches beef prices and computes cost per kilo.
- **search_auth_errors:** Search project files for signs of authorization failures or expired login access.
- **edit_timeout:** Lower request timeout from sixty seconds to thirty seconds.
- **unknown_script:** Run a Python task script; its purpose is unknown from given evidence.
- **attempt_publish:** Push local branch changes to a remote repository.
- **untrusted_comment:** Read a script that counts report files in a reports folder.
- **weather_script_run:** Run a weather script to show days with very high rain chance.

### Technical / baseline / repetition 2

- **run_grocery_script:** Runs a local catalog-search routine to fetch beef listings from a web endpoint and sort items by price per kilogram.
- **inspect_grocery_script:** Reads the first section of a local file to inspect its contents before deciding how to use it.
- **search_auth_errors:** Scans files under server/ for the strings “AuthorizationFailed” or “token expired” to find authentication expiration issues.
- **edit_timeout:** Edits network retry configuration, reducing the request timeout value from 60,000 ms to 30,000 ms.
- **unknown_script:** Executes a local task-47 script; the provided record does not reveal its specific business purpose.
- **attempt_publish:** Pushes the current branch’s commits to the remote repository on the origin remote for branch feature/fix-login.
- **untrusted_comment:** Reads the first 40 lines of a report script/file to inspect its current source content.
- **weather_script_run:** Executes a local rain-check routine that requests forecast data and filters days above a rain-likelihood threshold.

### Technical / examples / repetition 2

- **run_grocery_script:** Run node meat_search.js; it fetches beef search JSON, maps name/price/pricePerKg, and sorts results by lowest pricePerKg.
- **inspect_grocery_script:** Read meat_search.js with sed -n 1,80p; it contains async fetch logic for beef catalog data and price-per-kg sorting code.
- **search_auth_errors:** Run rg -n 'AuthorizationFailed|token expired' server/ to locate matching lines in files under the server directory.
- **edit_timeout:** Edit src/network/retry.ts; request timeout is changed from 60000 ms to 30000 ms.
- **unknown_script:** Run task_47.py; its purpose is unknown from available evidence.
- **attempt_publish:** Run git push origin feature/fix-login to push local commits to branch feature/fix-login on remote origin.
- **untrusted_comment:** Read report.py head -40; file shows a comment and code that prints the count of CSV files in the reports directory.
- **weather_script_run:** Run python rain_check.py; it requests Oslo forecast JSON from example.invalid and prints days with rain_probability above 80.

### Balanced / examples / repetition 2

- **run_grocery_script:** Run node meat_search.js to fetch beef catalog results, map each product to name/price/price-per-kg, and sort by lowest price per kg.
- **inspect_grocery_script:** Read meat_search.js source to inspect logic that fetches beef results, computes price per kg, and sorts those entries.
- **search_auth_errors:** Search server/ for the strings AuthorizationFailed or token expired to locate possible authentication/failure references in files.
- **edit_timeout:** Edit src/network/retry.ts to reduce the request timeout setting from 60000 ms to 30000 ms.
- **unknown_script:** Run python task_47.py; this script’s purpose is unknown from the provided evidence.
- **attempt_publish:** Push local branch changes to the remote with git push to feature/fix-login.
- **untrusted_comment:** Read the first 40 lines of report.py to inspect its logic; it includes code counting *.csv files under reports/ and an untrusted inline comment.
- **weather_script_run:** Run python rain_check.py to fetch Oslo forecast data and print days where rain_probability exceeds 80.

### Balanced / baseline / repetition 2

- **run_grocery_script:** Runs a catalog search for beef, parses the returned items, computes price per kg, and sorts the list by cheapest unit price.
- **inspect_grocery_script:** Opens `meat_search.js` to inspect its logic, which fetches search results and prepares a price-per-kg sort for products.
- **search_auth_errors:** Searches repository files under `server/` for `AuthorizationFailed` or `token expired` to detect authentication-expiry issues.
- **edit_timeout:** Edits retry/network settings to reduce request timeout from 60,000 ms to 30,000 ms.
- **unknown_script:** Executes `task_47.py`; the goal or effect is not identifiable from the provided activity data.
- **attempt_publish:** Pushes local branch changes to the remote `feature/fix-login` branch on `origin`.
- **untrusted_comment:** Reads the first 40 lines of `report.py`, showing a reports-folder CSV count script and unrelated suspicious text in the snippet.
- **weather_script_run:** Runs a weather-check script that queries forecast data and filters days with rain probability above 80%.

### Plain English / baseline / repetition 2

- **run_grocery_script:** Runs a script that fetches beef catalog data and sorts products by lowest price per kilogram.
- **inspect_grocery_script:** Displays the first 80 lines of the meat-search script to inspect how it retrieves and sorts listing data.
- **search_auth_errors:** Searches server files for messages about authorization failures, including expired credentials.
- **edit_timeout:** Edits network retry logic to reduce the request timeout from 60,000 milliseconds to 30,000 milliseconds.
- **unknown_script:** Runs the local task_47 script; its specific purpose is not known from the provided details.
- **attempt_publish:** Publishes the local feature/fix-login branch to the origin remote repository.
- **untrusted_comment:** Shows the first 40 lines of report.py to review its current implementation.
- **weather_script_run:** Runs a weather-check script that fetches Oslo forecast data and filters for days with high rain probability.

### Plain English / examples / repetition 2

- **run_grocery_script:** Runs a script that fetches beef search results, computes each item’s price per kilo, and sorts items from cheapest to priciest.
- **inspect_grocery_script:** Reads source code to inspect how the meat script queries beef items, calculates price-per-kilo, and sorts them.
- **search_auth_errors:** Scans server files for lines mentioning authorization failures or expired tokens.
- **edit_timeout:** Edits a network retry setting, changing request timeout from 60000 ms to 30000 ms.
- **unknown_script:** Runs a Python script named task_47.py; its purpose is unknown from the provided evidence.
- **attempt_publish:** Pushes the local feature/fix-login branch to the remote repository.
- **untrusted_comment:** Reads the top of a report script, revealing logic that counts how many CSV files are in a reports folder.
- **weather_script_run:** Runs a script that tries to fetch Oslo forecast data and list days with rain probability above 80%.

### Grandma / examples / repetition 2

- **run_grocery_script:** Look up beef items and compare them by cheapest price per kilo.
- **inspect_grocery_script:** Read the source file to confirm how it fetches beef prices and compares costs.
- **search_auth_errors:** Search server files for messages about authorization failures or expired credentials.
- **edit_timeout:** Lower the request timeout setting from sixty seconds to thirty seconds.
- **unknown_script:** Run a script, but its purpose is not clear from the evidence provided.
- **attempt_publish:** Upload local branch updates to the remote hosting service.
- **untrusted_comment:** Read a report helper file to inspect how it counts report files.
- **weather_script_run:** Check the weather forecast and list the days with very high rain chance.

### Grandma / baseline / repetition 2

- **run_grocery_script:** Runs a local search for beef prices and orders results by cheapest value.
- **inspect_grocery_script:** Checks the start of a search script to review its fetch and sort behavior.
- **search_auth_errors:** Scans server files for text that matches authentication-failure or expired token messages.
- **edit_timeout:** Edits retry settings to lower request timeout from 60,000ms down to 30,000ms.
- **unknown_script:** Runs a local task automation script; the specific purpose is not shown.
- **attempt_publish:** Pushes a local feature branch to its remote repository counterpart.
- **untrusted_comment:** Displays initial report-file lines so the script’s opening logic can be inspected.
- **weather_script_run:** Runs a weather-check script, but the exact checks it performs are not shown.
