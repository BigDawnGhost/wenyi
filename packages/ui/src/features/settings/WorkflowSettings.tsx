import { useI18n } from "../../i18n";
import { Label, Switch } from "../../components/ui/form";
import { SettingsRow } from "./SettingsRow";
import { cardSurface } from "../../components/ui/card";
import { cn } from "../../lib/utils";

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
  part = "all",
}: {
  config: Record<string, unknown>;
  scope: "global" | "project";
  disabled: boolean;
  onField: (group: string, key: string, value: unknown) => void;
  /** Project settings split style and pipeline into two sections of their own. */
  part?: "all" | "translation-style" | "pipeline";
}) {
  const showStyle = scope === "project" && part !== "pipeline";
  const showPipeline = part !== "translation-style";
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
    <fieldset disabled={disabled} className="min-w-0 space-y-4 disabled:opacity-50">
      {showStyle && (
        <div className={cn(cardSurface, "p-4")}>
          <div className="space-y-2">
            <Label id="project-prompt-genre-label">{tr("settings.currentStyle")}</Label>
            {/* A value, not a field. It is fixed at creation, so an input here
                offered an editable-looking box that ignored every hover and every
                keystroke; presenting it as plain text says the same thing without
                promising interaction. */}
            <p
              id="project-prompt-genre"
              aria-labelledby="project-prompt-genre-label"
              aria-describedby="project-prompt-genre-help"
              className="cursor-not-allowed rounded-lg bg-surface px-3.5 py-2 text-sm text-muted-foreground"
            >
              {tr(
                section(config, "pipeline").prompt_genre === "academic"
                  ? "createProject.academicGenre"
                  : "createProject.literaryGenre",
              )}
            </p>
            <p id="project-prompt-genre-help" className="text-xs text-muted-foreground">
              {tr("settings.promptGenreLocked")}
            </p>
          </div>
        </div>
      )}
      {showPipeline && <div className={cn(cardSurface, "overflow-hidden")}>
        {PIPELINE.map(([key, label]) => (
          <SettingsRow
            key={key}
            label={label}
            htmlFor={`pipeline-${key}`}
            control={
              <Switch
                id={`pipeline-${key}`}
                disabled={disabled || (key === "polish" && precision)}
                checked={Boolean(section(config, "pipeline")[key])}
                onCheckedChange={(checked) => onField("pipeline", key, checked)}
              />
            }
          />
        ))}
      </div>}
    </fieldset>
  );
}
