// Whether the state inspector's panel is open. The panel (StateInspector) and
// the button that opens it (StateToggle) live apart: the button sits in the
// layout (the sidebar's foot, the phone dock), never floating over a control.
export const inspector = $state({ open: false });

export function toggleInspector() {
  inspector.open = !inspector.open;
}
