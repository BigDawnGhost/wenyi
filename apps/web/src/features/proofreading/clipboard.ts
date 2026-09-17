export async function copyText(text: string): Promise<void> {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch {
    // Local HTTP deployments may deny the async clipboard API.
  }
  const selection = window.getSelection();
  const ranges = selection
    ? Array.from({ length: selection.rangeCount }, (_, i) =>
        selection.getRangeAt(i).cloneRange(),
      )
    : [];
  const active = document.activeElement;
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("aria-hidden", "true");
  input.style.cssText =
    "position:fixed;left:0;top:0;opacity:0;pointer-events:none";
  document.body.append(input);
  try {
    input.focus({ preventScroll: true });
    input.select();
    if (!document.execCommand("copy")) throw new Error("Clipboard unavailable");
  } finally {
    input.remove();
    if (active instanceof HTMLElement && active.isConnected)
      active.focus({ preventScroll: true });
    selection?.removeAllRanges();
    for (const range of ranges)
      if (range.commonAncestorContainer.isConnected) selection?.addRange(range);
  }
}
