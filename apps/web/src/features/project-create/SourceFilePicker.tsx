import { useRef, useState } from "react";
import { FolderOpen } from "lucide-react";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/data";

export function SourceFilePicker({
  filename,
  extensions,
  disabled,
  onSelectFile,
}: {
  filename?: string;
  extensions: string[];
  disabled: boolean;
  onSelectFile: (file: File) => void;
}) {
  const { t } = useI18n();
  const input = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const [dragging, setDragging] = useState(false);
  const [multipleFiles, setMultipleFiles] = useState(false);
  const selectFile = (file: File) => {
    setMultipleFiles(false);
    onSelectFile(file);
  };
  return (
    <div
      role="group"
      aria-label={t("createProject.sourceDropZone")}
      aria-disabled={disabled}
      className={cn(
        "space-y-3 rounded-lg border border-dashed p-4 transition-colors",
        dragging && !disabled && "border-foreground bg-muted",
      )}
      onDragEnter={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        if (disabled) return;
        dragDepth.current += 1;
        setDragging(true);
      }}
      onDragOver={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = disabled ? "none" : "copy";
      }}
      onDragLeave={() => {
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        if (disabled) return;
        const files = event.dataTransfer.files;
        if (files.length > 1) {
          setMultipleFiles(true);
        } else if (files.length === 1) {
          selectFile(files[0]);
        }
      }}
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
          {filename || t("createProject.noFileSelected")}
        </span>
      </div>
      <ErrorNotice
        error={multipleFiles ? t("createProject.singleFileOnly") : undefined}
      />
    </div>
  );
}
