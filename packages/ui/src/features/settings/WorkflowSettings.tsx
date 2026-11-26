import { useI18n } from "../../i18n";
import { Label, Switch } from "../../components/ui/form";
import { Select, SelectItem } from "../../components/ui/select";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";
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
  const pipeline = section(config, "pipeline");
  const precision =
    scope === "project" &&
    pipeline.translation_mode === "three_draft_synthesis";
  const polishEnabled = Boolean(pipeline.polish);
  const polishMode = pipeline.polish_mode === "sparse" ? "sparse" : "full";
  const chapterConcurrencyNum = Number(pipeline.chapter_concurrency);
  const chapterConcurrency =
    Number.isInteger(chapterConcurrencyNum) && chapterConcurrencyNum >= 1
      ? String(chapterConcurrencyNum)
      : "1";
  const chapterConcurrencyOptions = Array.from(
    new Set(["1", "2", "4", "8", chapterConcurrency]),
  ).sort((a, b) => Number(a) - Number(b));

  const PIPELINE: [string, string, string | undefined][] = [
    ["book_understanding", tr("settings.bookUnderstanding"), undefined],
    ["polish", tr("settings.polishing"), undefined],
    ["review", tr("common.wholeBookReview"), undefined],
    ["review_autofix", tr("settings.applyAutofixesToTheSavedTranslation"), undefined],
    ["skip_boilerplate", tr("settings.skipBoilerplate"), tr("settings.skipBoilerplateHelp")],
  ];

  return (
    <fieldset disabled={disabled} className="min-w-0 space-y-4 disabled:opacity-50">
      {showStyle && (
        <div className={cn(cardSurface, "p-4")}>
          <div className="space-y-2">
            <Label id="project-prompt-genre-label">{tr("settings.currentStyle")}</Label>
            <p
              id="project-prompt-genre"
              aria-labelledby="project-prompt-genre-label"
              aria-describedby="project-prompt-genre-help"
              className="cursor-not-allowed rounded-lg bg-surface px-3.5 py-2 text-sm text-muted-foreground"
            >
              {tr(
                pipeline.prompt_genre === "academic"
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
      {showPipeline && (
        <div className={cn(cardSurface, "overflow-hidden")}>
          {PIPELINE.map(([key, label, help]) => (
            <SettingsRow
              key={key}
              label={label}
              description={help}
              htmlFor={`pipeline-${key}`}
              control={
                <Switch
                  id={`pipeline-${key}`}
                  disabled={disabled || (key === "polish" && precision)}
                  checked={Boolean(pipeline[key])}
                  onCheckedChange={(checked) => onField("pipeline", key, checked)}
                />
              }
            />
          ))}
          <SettingsRow
            label={tr("settings.polishMode")}
            description={tr("settings.polishModeHelp")}
            htmlFor="pipeline-polish_mode"
            control={
              <Select
                id="pipeline-polish_mode"
                value={polishMode}
                disabled={disabled || !polishEnabled || precision}
                onValueChange={(value) => onField("pipeline", "polish_mode", value)}
                className={SETTINGS_CONTROL_CLASSNAME}
              >
                <SelectItem value="full">{tr("settings.polishModeFull")}</SelectItem>
                <SelectItem value="sparse">{tr("settings.polishModeSparse")}</SelectItem>
              </Select>
            }
          />
          <SettingsRow
            label={tr("settings.chapterConcurrency")}
            description={tr("settings.chapterConcurrencyHelp")}
            htmlFor="pipeline-chapter_concurrency"
            control={
              <Select
                id="pipeline-chapter_concurrency"
                value={chapterConcurrency}
                disabled={disabled || precision}
                onValueChange={(value) =>
                  onField("pipeline", "chapter_concurrency", Number(value))
                }
                className={SETTINGS_CONTROL_CLASSNAME}
              >
                {chapterConcurrencyOptions.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option}
                  </SelectItem>
                ))}
              </Select>
            }
          />
        </div>
      )}
    </fieldset>
  );
}
