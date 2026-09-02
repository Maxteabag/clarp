// Ink-bleed press effect.
//
// Listens for pointerdown on elements carrying the `data-haptic` attribute
// (or matching a selector list below) and spawns a small radial-gradient
// "ink bloom" at the touch point. The bloom is rendered as an absolutely-
// positioned child of the button — callers must give the button
// `position: relative; overflow: hidden;` (covered by styles.css).
//
// Honors prefers-reduced-motion by skipping the bloom entirely; the
// underlying button still gets its :active CSS state, which is what
// reduced-motion users implicitly opt in to (CSS already collapses
// animation-duration to ~0 under that media query).

// Only ACTION buttons get the ink bloom. The agent chip (.session) is a
// stateful navigation element, not an action — painting a red splash on
// it under the avatar/name looks like a bug. Keep it bloom-free.
const SELECTOR =
  ".btn-haptic, .mic, .history-toggle, .chat-btn, .mute-audio-btn, " +
  ".stop-btn, .ctrlc-btn, .chat-send, .chat-close, .overview-close, " +
  ".row-action, .history-btn, .switcher-item, .start-btn, .voice-pick, " +
  ".voice-preview";

const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function spawnBloom(target, x, y) {
  if (reduced) return;
  const rect = target.getBoundingClientRect();
  const localX = x - rect.left;
  const localY = y - rect.top;
  // Bloom diameter = ~1.4 × the longest side of the button so the ink
  // visibly soaks past the edges before being clipped by overflow:hidden.
  const size = Math.max(rect.width, rect.height) * 1.4;
  const bloom = document.createElement("span");
  bloom.className = "press-bloom";
  bloom.style.left = `${localX - size / 2}px`;
  bloom.style.top = `${localY - size / 2}px`;
  bloom.style.width = `${size}px`;
  bloom.style.height = `${size}px`;
  target.appendChild(bloom);
  // Force a reflow so the transition picks up the .show class change.
  void bloom.offsetWidth;
  bloom.classList.add("show");
  // Total lifetime: 360ms. Fade-out begins at 220ms.
  setTimeout(() => bloom.classList.add("fading"), 220);
  setTimeout(() => bloom.remove(), 420);
}

function onDown(e) {
  const t = e.target.closest(SELECTOR);
  if (!t) return;
  // Skip disabled buttons.
  if (t.disabled) return;
  spawnBloom(t, e.clientX, e.clientY);
}

window.addEventListener("pointerdown", onDown, { passive: true });
