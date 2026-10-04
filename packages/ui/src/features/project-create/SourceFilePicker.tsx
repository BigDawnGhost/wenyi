import { useEffect, useRef, useState } from "react";
import { FolderOpen } from "lucide-react";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/data";
import { platform, type ProjectSource } from "@/platform";

export function SourceFilePicker({
  filename,
  size,
  extensions,
  disabled,
  onSelectFile,
}: {
  filename?: string;
  size?: number;
  extensions: string[];
  disabled: boolean;
  onSelectFile: (file: ProjectSource) => void;
}) {
  const { t } = useI18n();
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
      className={cn(
        "space-y-3 rounded-lg border border-dashed p-4 transition-colors",
        dragging && !disabled && "border-foreground bg-muted",
      )}
    >
      {!disabled && (
        <p role="status" className="text-sm text-muted-foreground">
          {t(
            dragging
              ? "createProject.releaseFile"
              : "createProject.dropFileHint",
          )}
        </p>
      )}
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
      <div className="flex flex-wrap items-center gap-3">
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
        <span
          id="source-file-name"
          aria-live="polite"
          className="min-w-0 text-sm text-muted-foreground [overflow-wrap:anywhere]"
        >
          <span>{filename || t("createProject.noFileSelected")}</span>
          {size !== undefined && ` (${size.toLocaleString()} bytes)`}
        </span>
      </div>
      <ErrorNotice error={selectionError} />
    </div>
  );
}
