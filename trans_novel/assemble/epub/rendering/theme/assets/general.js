function classify(node, document) {
  const types = Array.isArray(node.epubTypes) ? node.epubTypes : [];
  if (types.includes("subtitle")) return { role: "subtitle" };
  if (types.includes("caption") || node.tag === "figcaption") return { role: "caption" };

  const nativeHeading = /^h[1-6]$/.test(node.tag);
  const chineseNumber = "第[〇零一二三四五六七八九十百千万两廿卅0-9０-９]+[章节卷部篇]";
  const englishNumber = "(?:chapter|part)\\s+(?:[0-9]+|[ivxlcdm]+)";
  const numberOnly = new RegExp("^(?:" + chineseNumber + "|" + englishNumber + ")$", "iu");
  const numberWithTitle = new RegExp(
    "^(?:" + chineseNumber + "|" + englishNumber + ")(?:[\\s:：.。—–-]+)(\\S.*)$",
    "iu"
  );

  if (nativeHeading && !node.textTruncated && numberOnly.test(node.text)) {
    return { role: "chapter-number" };
  }
  if (nativeHeading) return { role: "heading", level: Number(node.tag.slice(1)) };
  if (
    node.ariaRole === "heading" &&
    Number.isInteger(node.ariaLevel) &&
    node.ariaLevel >= 1 &&
    node.ariaLevel <= 6
  ) {
    return { role: "heading", level: node.ariaLevel };
  }

  const standalone =
    node.isTextBlock &&
    !node.textTruncated &&
    node.textLength <= 80 &&
    !node.context.inTable &&
    !node.context.inList &&
    !node.context.inNavigation;
  if (standalone) {
    if (numberOnly.test(node.text)) return { role: "chapter-number" };
    if (numberWithTitle.test(node.text)) return { role: "heading", level: 1 };
  }

  if (node.tag === "p") {
    if (node.context.inQuote) return { role: "quote-text" };
    if (node.context.inTable) return { role: "table-text" };
    if (node.context.inList) return { role: "list-text" };
    return { role: "body" };
  }
  if (node.tag === "blockquote") return { role: "quote" };
  return null;
}
