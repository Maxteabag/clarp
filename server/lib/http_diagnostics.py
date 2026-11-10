"""Private loss boundary for five HTTP diagnostic failures (Wave2).

Only fixed codes go to fd2: no exception text, request data or recursive log
sink. One <=96-byte attempt is NOT a time bound: blocking stderr can prevent
return. A full count means ATTEMPTED, never a delivery receipt. Callers discard
the result; it is not independent evidence when every channel is unavailable.
"""
from __future__ import annotations

import os
import sqlite3


# H01 peer-disconnect log; H02 telemetry finish; H03 HTTP observation;
# H04 SSE eviction log; H05 SSE disconnect log. Rows/categories are immutable.
_SITES = ("H01", "H02", "H03", "H04", "H05")
_PAYLOADS = (
    (b"clarpHttpDiagnostic site=H01 error=IO\n",
     b"clarpHttpDiagnostic site=H01 error=DB\n",
     b"clarpHttpDiagnostic site=H01 error=VALUE\n",
     b"clarpHttpDiagnostic site=H01 error=IMPORT\n",
     b"clarpHttpDiagnostic site=H01 error=RUNTIME\n",
     b"clarpHttpDiagnostic site=H01 error=OTHER\n"),
    (b"clarpHttpDiagnostic site=H02 error=IO\n",
     b"clarpHttpDiagnostic site=H02 error=DB\n",
     b"clarpHttpDiagnostic site=H02 error=VALUE\n",
     b"clarpHttpDiagnostic site=H02 error=IMPORT\n",
     b"clarpHttpDiagnostic site=H02 error=RUNTIME\n",
     b"clarpHttpDiagnostic site=H02 error=OTHER\n"),
    (b"clarpHttpDiagnostic site=H03 error=IO\n",
     b"clarpHttpDiagnostic site=H03 error=DB\n",
     b"clarpHttpDiagnostic site=H03 error=VALUE\n",
     b"clarpHttpDiagnostic site=H03 error=IMPORT\n",
     b"clarpHttpDiagnostic site=H03 error=RUNTIME\n",
     b"clarpHttpDiagnostic site=H03 error=OTHER\n"),
    (b"clarpHttpDiagnostic site=H04 error=IO\n",
     b"clarpHttpDiagnostic site=H04 error=DB\n",
     b"clarpHttpDiagnostic site=H04 error=VALUE\n",
     b"clarpHttpDiagnostic site=H04 error=IMPORT\n",
     b"clarpHttpDiagnostic site=H04 error=RUNTIME\n",
     b"clarpHttpDiagnostic site=H04 error=OTHER\n"),
    (b"clarpHttpDiagnostic site=H05 error=IO\n",
     b"clarpHttpDiagnostic site=H05 error=DB\n",
     b"clarpHttpDiagnostic site=H05 error=VALUE\n",
     b"clarpHttpDiagnostic site=H05 error=IMPORT\n",
     b"clarpHttpDiagnostic site=H05 error=RUNTIME\n",
     b"clarpHttpDiagnostic site=H05 error=OTHER\n"),
)


def _note_http_failure(site: str, error: Exception) -> str:
    """One best-effort attempt; ordinary faults mean diagnostic loss.

    The final Exception boundary protects only this diagnostic operation, not
    the caller's original operation. It deliberately does not catch
    BaseException/cancellation outside Exception. Partial bytes may escape;
    no retry/flush/second sink occurs even for InterruptedError.
    """
    try:
        if type(site) is not str or site not in _SITES:
            return "DROPPED"
        if not isinstance(error, Exception):
            return "DROPPED"
        if isinstance(error, OSError):
            category = 0
        elif isinstance(error, sqlite3.Error):
            category = 1
        elif isinstance(error, ValueError):
            category = 2
        elif isinstance(error, ImportError):
            category = 3
        elif isinstance(error, RuntimeError):
            category = 4
        else:
            category = 5
        payload = _PAYLOADS[_SITES.index(site)][category]
        count = os.write(2, payload)
        if type(count) is int and count == len(payload):
            return "ATTEMPTED"
        return "DROPPED"
    except Exception:  # explicit diagnostic-loss boundary; NOT delivery proof
        return "DROPPED"
