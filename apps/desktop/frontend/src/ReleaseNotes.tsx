import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Dialog } from "@wenyi/ui/primitives";
import { useDesktopI18n } from "./i18n";
import { remarkLocalizedReleaseNotes } from "./localizedReleaseNotes";

export function ReleaseNotes({ open, onClose, version, notes }: {
  open: boolean;
  onClose: () => void;
  version?: string;
  notes?: string;
}) {
  const { locale, t } = useDesktopI18n();
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={t("updates.notesTitle")}
      description={version ? t("updates.version", { version }) : undefined}
      className="max-w-2xl"
    >
      <div
        lang={locale}
        className="space-y-4 break-words text-sm leading-relaxed
          [&_h1]:text-xl [&_h2]:text-lg [&_h3]:text-base
          [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold
          [&_h4]:font-semibold [&_h5]:font-semibold [&_h6]:font-semibold
          [&_ul]:list-disc [&_ol]:list-decimal [&_ul]:pl-6 [&_ol]:pl-6 [&_li]:my-1
          [&_blockquote]:border-l-2 [&_blockquote]:pl-4 [&_blockquote]:text-muted-foreground
          [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:font-mono
          [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-3
          [&_pre_code]:bg-transparent [&_pre_code]:p-0
          [&_table]:w-full [&_table]:border-collapse [&_th]:text-left
          [&_th]:border [&_td]:border [&_th]:p-2 [&_td]:p-2"
      >
        <Markdown
          skipHtml
          remarkRehypeOptions={{ footnoteLabel: t("updates.footnotes") }}
          remarkPlugins={[
            remarkGfm,
            [remarkLocalizedReleaseNotes, { locale, emptyMessage: t("updates.notesEmpty") }],
          ]}
          components={{
            // The native host denies external navigation. Keep notes read-only.
            a: ({ children }) => <span>{children}</span>,
            img: ({ alt }) => <span>{alt}</span>,
            table: ({ children }) => <div className="overflow-x-auto"><table>{children}</table></div>,
          }}
        >
          {notes || ""}
        </Markdown>
      </div>
    </Dialog>
  );
}
