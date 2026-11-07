import { useState } from "react";
import { FolderOpen } from "lucide-react";
import { useI18n } from "../../i18n";
import { Button } from "../../components/ui/button";
import { Input, Label } from "../../components/ui/form";
import { ErrorNotice } from "../../components/ui/data";
import { platform } from "../../platform";

/** Choose where a new project is written, or leave the installation default. */
export function ProjectLocationPicker({
  location,
  disabled,
  onChange,
}: {
  /** The chosen folder, or null while the default is in force. */
  location: string | null;
  disabled: boolean;
  onChange: (location: string | null) => void;
}) {
  const { t } = useI18n();
  const [failure, setFailure] = useState<string>();
  const library = platform().capabilities.library;
  // No picker means no choice to make: the Web host has no filesystem, so the
  // whole control is absent rather than shown and disabled.
  if (!library?.choose) return null;
  const choose = async () => {
    setFailure(undefined);
    try {
      const chosen = await library.choose();
      if (chosen) onChange(chosen);
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    }
  };
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-3">
        <Label htmlFor="project-location">{t("createProject.location")}</Label>
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Input
            id="project-location"
            readOnly
            value={location ?? ""}
            placeholder={t("createProject.locationDefault")}
            aria-label={t("createProject.location")}
            disabled={disabled}
            className="min-w-0 flex-1 basis-48"
          />
          <Button
            type="button"
            variant="outline"
            disabled={disabled}
            onClick={() => void choose()}
          >
            <FolderOpen className="h-4 w-4" aria-hidden="true" />
            {t("createProject.chooseLocation")}
          </Button>
          {location && (
            <Button
              type="button"
              variant="ghost"
              disabled={disabled}
              onClick={() => onChange(null)}
            >
              {t("createProject.locationUseDefault")}
            </Button>
          )}
        </div>
      </div>
      <ErrorNotice error={failure} />
    </div>
  );
}
