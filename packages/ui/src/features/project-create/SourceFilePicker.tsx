import { useEffect, useRef, useState } from "react";
import { FileText, FolderOpen } from "lucide-react";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/data";
import { platform, type ProjectSource } from "@/platform";

function formatSize(bytes: number, locale: string) {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  const digits = unit === 0 || value >= 10 ? 0 : 1;
  return `${value.toLocaleString(locale, { maximumFractionDigits: digits })} ${units[unit]}`;
}

export function SourceFilePicker({
  filename,
  size,
  extensions,
  formats,
  disabled,
  onSelectFile,
}: {
  filename?: string;
  size?: number;
  extensions: string[];
  /** Display names of supported formats, shown inside the empty zone. */
  formats: string[];
  disabled: boolean;
  onSelectFile: (file: ProjectSource) => void;
}) {
  const { t, locale } = useI18n();
  const input = useRef<HTMLInputElement>(null);
  const zone = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);
  const [selectionError, setSelectionError] = useState<string>();
  const selectFile = (file: ProjectSource) => {
    if (disabled) return false;
    if (!file.size) {
      setSelectionError(t("createProject.emptyFile"));
      return false;
    }
    if (!extensions.includes(file.name.split(".").pop()?.toLowerCase() || "")) {
      setSelectionError(t("createProject.unsupportedFile"));
      return false;
    }
    setSelectionError(undefined);
    onSelectFile(file);
    return true;
  };
  useEffect(() => {
    if (disabled) setDragging(false);
  }, [disabled]);
  const selection = useRef({ disabled, selectFile });
  selection.current = { disabled, selectFile };
  useEffect(() => {
    if (!zone.current) return;
    return platform().bindSourceDrop(zone.current, {
      get disabled() { return selection.current.disabled; },
      select: (source) => selection.current.selectFile(source),
      dragging: setDragging,
      error: setSelectionError,
    });
  }, []);
  return (
    <div
      ref={zone}
      role="group"
      aria-label={t("createProject.sourceDropZone")}
      aria-disabled={disabled}
      className="space-y-2"
    >
      <div
        className={cn(
          "flex min-h-24 flex-wrap items-center gap-3 rounded-lg border border-dashed px-4 py-4 transition-colors",
          dragging && !disabled && "border-foreground bg-muted",
          filename && "border-solid",
        )}
      >
        <input
          ref={input}
          hidden
          aria-label={t("createProject.uploadSource")}
          type="file"
          accept={extensions.map((extension) => `.${extension}`).join(",")}
          disabled={disabled}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) {
              selectFile(file);
              event.currentTarget.value = "";
            }
          }}
        />
        {filename ? (
          <FileText className="h-5 w-5 shrink-0 text-muted-foreground" aria-hidden="true" />
        ) : null}
        <div
          id="source-file-name"
          aria-live="polite"
          className="min-w-0 flex-1 basis-40 text-sm [overflow-wrap:anywhere]"
        >
          {dragging && !disabled ? (
            <span role="status">{t("createProject.releaseFile")}</span>
          ) : filename ? (
            <>
              <span className="font-medium">{filename}</span>
              {size !== undefined && (
                <span className="ml-2 text-muted-foreground">{formatSize(size, locale)}</span>
              )}
            </>
          ) : (
            <>
              <span className="block">{t("createProject.dropFileHint")}</span>
              {formats.length > 0 && (
                <span className="mt-1 block text-xs uppercase tracking-wide text-muted-foreground">
                  {formats.join(" · ")}
                </span>
              )}
            </>
          )}
        </div>
        <Button
          type="button"
          variant="outline"
          className="shrink-0"
          disabled={disabled}
          aria-describedby="source-file-name"
          onClick={() => input.current?.click()}
        >
          <FolderOpen className="h-4 w-4" aria-hidden="true" />
          {t("createProject.browseFiles")}
        </Button>
      </div>
      <ErrorNotice error={selectionError} />
    </div>
  );
}
