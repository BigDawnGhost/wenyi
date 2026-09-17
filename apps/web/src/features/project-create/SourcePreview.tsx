import { useI18n } from "@/i18n";
import type { UploadPreview } from "@/lib/api";

export function SourcePreview({ preview }: { preview: UploadPreview }) {
  const { t: tr } = useI18n();
  return (
    <div className="rounded border p-4 space-y-3">
      <h3 className="font-medium">{preview.title}</h3>
      <p className="text-sm">
        {preview.fmt.toUpperCase()} ·{" "}
        {preview.fmt === "srt"
          ? tr("createProject.subtitleCues", {
              count: preview.total_word_count,
            })
          : tr("createProject.chaptersParagraphs", {
              chapters: preview.chapter_count,
              count: preview.total_word_count,
            })}
      </p>
      <details>
        <summary className="cursor-pointer text-sm">
          {tr("createProject.viewParsedStructure")}
        </summary>
        <ol className="mt-2 max-h-56 overflow-auto text-sm space-y-2">
          {preview.chapters.map((chapter) => (
            <li key={chapter.index}>
              {chapter.title || tr("createProject.untitled")}{" "}
              <span className="text-muted-foreground">
                {tr("createProject.paragraphCount", {
                  count: chapter.word_count,
                })}
              </span>
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}
