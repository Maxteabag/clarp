// Status hysteresis: a new status shows only once it has lasted `delayMs`,
// and a shown one stays at least `holdMs`, so a fast tool or a 100 ms
// thinking blip never flashes the status line. Values carry a `key`; a new
// value with the shown key (the same state, a newer headline or timer) is
// applied at once.

export const STATUS_DELAY_MS = 400;
export const STATUS_HOLD_MS = 400;

export function createDelayedStatus({
  onChange = () => {}, delayMs = STATUS_DELAY_MS, holdMs = STATUS_HOLD_MS,
  now = () => Date.now(),
} = {}) {
  let shown = null;
  let shownAt = 0;
  let target = null;
  let timer = null;

  const keyOf = v => (v ? v.key : null);
  const same = (a, b) => a === b || (!!a && !!b
    && Object.keys(a).length === Object.keys(b).length
    && Object.keys(a).every(k => a[k] === b[k]));

  function cancel() {
    if (timer) { clearTimeout(timer); timer = null; }
  }

  function show() {
    timer = null;
    if (keyOf(target) === keyOf(shown)) return;
    shown = target;
    shownAt = now();
    onChange(shown);
  }

  function set(value) {
    target = value || null;
    if (keyOf(target) === keyOf(shown)) {
      cancel();
      if (!same(target, shown)) {
        shown = target;
        onChange(shown);
      }
      return;
    }
    // Already waiting to replace the shown status: keep that deadline, so a
    // target that keeps changing still lands within the delay (show() takes
    // whatever the target is by then).
    if (timer) return;
    const held = shown ? Math.max(0, shownAt + holdMs - now()) : 0;
    const wait = target ? Math.max(delayMs, held) : held;
    if (wait <= 0) show();
    else timer = setTimeout(show, wait);
  }

  return {
    set,
    current: () => shown,
    dispose: cancel,
  };
}
