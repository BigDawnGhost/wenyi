import type { Root } from "mdast";
import { toString } from "mdast-util-to-string";
import type { Locale } from "@wenyi/ui/i18n";

const languageLabels: Record<Locale, readonly string[]> = {
  en: ["english", "en"],
  "zh-CN": ["中文", "简体中文", "zh-cn"],
};

/** Select one language section from the parsed Markdown, never from code blocks. */
export function remarkLocalizedReleaseNotes({
  locale,
  emptyMessage,
}: {
  locale: Locale;
  emptyMessage: string;
}) {
  return (tree: Root) => {
    const start = tree.children.findIndex(node =>
      node.type === "heading" &&
      languageLabels[locale].includes(toString(node).trim().toLowerCase()),
    );
    const heading = tree.children[start];
    let end = start + 1;
    if (heading?.type === "heading") {
      // Releases use a divider before another language or a shared bilingual footer.
      while (end < tree.children.length) {
        const node = tree.children[end];
        if (node.type === "thematicBreak" ||
          (node.type === "heading" && node.depth <= heading.depth)) break;
        end++;
      }
    }
    const selected = start < 0 ? [] : tree.children.slice(start + 1, end);
    // Reference-style links can have definitions outside the language section.
    const definitions = tree.children.filter(node => node.type === "definition");
    tree.children = selected.some(node => node.type !== "definition")
      ? [...selected.filter(node => node.type !== "definition"), ...definitions]
      : [{ type: "paragraph", children: [{ type: "text", value: emptyMessage }] }];
  };
}
