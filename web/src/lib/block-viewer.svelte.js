// What the full screen block viewer shows (BlockViewer.svelte), or null.
// { kind: 'code' | 'table', lang?, text, html }
//   text: what copy puts on the clipboard (the code, or the table's Markdown)
//   html: the chat's own markup for it (highlighted code, or the <table>)

export const blockViewer = $state({ item: null });

export function openBlockViewer(item) {
  blockViewer.item = item;
}

export function closeBlockViewer() {
  blockViewer.item = null;
}
