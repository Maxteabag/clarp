// Coalesce transcript wakeups into bounded agent-snapshot refreshes.

export function createCoalescedRefresh(refresh, {
  delayMs = 200,
  setTimeoutFn = setTimeout,
} = {}) {
  let timer = null;
  let inFlight = false;
  let pending = false;

  async function flush() {
    timer = null;
    if (!pending || inFlight) return;
    pending = false;
    inFlight = true;
    try {
      await refresh();
    } finally {
      inFlight = false;
      if (pending && !timer) timer = setTimeoutFn(flush, delayMs);
    }
  }

  function schedule() {
    pending = true;
    if (!timer && !inFlight) timer = setTimeoutFn(flush, delayMs);
  }

  return { schedule };
}
