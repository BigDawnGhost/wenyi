import { useI18n } from "../../i18n";
import { Input, Label } from "../../components/ui/form";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";

const section = (config: Record<string, unknown>, key: string) =>
  (config[key] || {}) as Record<string, unknown>;

/** Retain creation-time choices and advanced settings when changing a visible field. */
export function workflowField(
  config: Record<string, unknown>,
  group: string,
  key: string,
  value: unknown,
) {
  return {
    ...config,
    [group]: {
      ...section(config, group),
      [key]: value,
    },
  };
}

export function WorkflowSettings({
  config,
  scope,
  disabled,
  onField,
}: {
  config: Record<string, unknown>;
  scope: "global" | "project";
  disabled: boolean;
  onField: (group: string, key: string, value: unknown) => void;
}) {
  const { t: tr } = useI18n();
  const precision =
    scope === "project" &&
    section(config, "pipeline").translation_mode === "three_draft_synthesis";
  const PIPELINE: [string, string][] = [
    ["book_understanding", tr("settings.bookUnderstanding")],
    ["polish", tr("settings.polishing")],
    ["review", tr("common.wholeBookReview")],
    ["review_autofix", tr("settings.applyAutofixesToTheSavedTranslation")],
  ];

  return (
    <fieldset disabled={disabled} className="min-w-0 space-y-4 disabled:opacity-60">
      {scope === "project" && (
        <div className="space-y-2">
          <Label htmlFor="project-prompt-genre">{tr("createProject.promptGenre")}</Label>
          <Input
            id="project-prompt-genre"
            className={SETTINGS_CONTROL_CLASSNAME}
            readOnly
            value={tr(
              section(config, "pipeline").prompt_genre === "academic"
                ? "createProject.academicGenre"
                : "createProject.literaryGenre",
            )}
            aria-describedby="project-prompt-genre-help"
          />
          <p id="project-prompt-genre-help" className="text-xs text-muted-foreground">
            {tr("settings.promptGenreLocked")}
          </p>
        </div>
      )}
      <div className="overflow-hidden rounded-xl border border-border">
        {PIPELINE.map(([key, label]) => (
          <SettingsRow
            key={key}
            label={label}
            htmlFor={`pipeline-${key}`}
            control={
              <input
                type="checkbox"
                id={`pipeline-${key}`}
                disabled={key === "polish" && precision}
                checked={Boolean(section(config, "pipeline")[key])}
                onChange={(e) => onField("pipeline", key, e.target.checked)}
              />
            }
          />
        ))}
      </div>
    </fieldset>
  );
}
