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
  const glossaryMode = pipeline.glossary_mode === "compiled" ? "compiled" : "incremental";
  const polishMode = pipeline.polish_mode === "sparse" ? "sparse" : "full";
  const chapterConcurrencyNum = Number(pipeline.chapter_concurrency);
  const chapterConcurrency =
    Number.isInteger(chapterConcurrencyNum) && chapterConcurrencyNum >= 1
      ? String(chapterConcurrencyNum)
      : "1";
  const concurrencyOptions = Array.from(
    new Set(["1", "2", "4", "8", chapterConcurrency]),
  ).sort((a, b) => Number(a) - Number(b));
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
                checked={Boolean(pipeline[key])}
                onCheckedChange={(checked) => onField("pipeline", key, checked)}
              />
            }
          />
        ))}
        <SettingsRow
          label={tr("settings.consistencySweep")}
          htmlFor="pipeline-consistency_sweep"
          description={tr("settings.consistencySweepHelp")}
          control={
            <Switch
              id="pipeline-consistency_sweep"
              disabled={disabled}
              checked={Boolean(pipeline.consistency_sweep)}
              onCheckedChange={(checked) => onField("pipeline", "consistency_sweep", checked)}
            />
          }
        />
        <SettingsRow
          label={tr("settings.deterministicHygiene")}
          htmlFor="pipeline-deterministic_hygiene"
          description={tr("settings.deterministicHygieneHelp")}
          control={
            <Switch
              id="pipeline-deterministic_hygiene"
              disabled={disabled}
              checked={pipeline.deterministic_hygiene !== false}
              onCheckedChange={(checked) => onField("pipeline", "deterministic_hygiene", checked)}
            />
          }
        />
        <SettingsRow
          label={tr("settings.glossaryMode")}
          htmlFor="pipeline-glossary-mode"
          description={tr("settings.glossaryModeHelp")}
          control={
            <Select
              id="pipeline-glossary-mode"
              className={SETTINGS_CONTROL_CLASSNAME}
              disabled={disabled}
              value={glossaryMode}
              onValueChange={(value) => onField("pipeline", "glossary_mode", value)}
            >
              <SelectItem value="incremental">{tr("settings.glossaryModeIncremental")}</SelectItem>
              <SelectItem value="compiled">{tr("settings.glossaryModeCompiled")}</SelectItem>
            </Select>
          }
        />
        <SettingsRow
          label={tr("settings.polishMode")}
          htmlFor="pipeline-polish-mode"
          description={tr("settings.polishModeHelp")}
          control={
            <Select
              id="pipeline-polish-mode"
              className={SETTINGS_CONTROL_CLASSNAME}
              disabled={disabled || !pipeline.polish || precision}
              value={polishMode}
              onValueChange={(value) => onField("pipeline", "polish_mode", value)}
            >
              <SelectItem value="full">{tr("settings.polishModeFull")}</SelectItem>
              <SelectItem value="sparse">{tr("settings.polishModeSparse")}</SelectItem>
            </Select>
          }
        />
        <SettingsRow
          label={tr("settings.chapterConcurrency")}
          htmlFor="pipeline-chapter-concurrency"
          description={tr("settings.chapterConcurrencyHelp")}
          control={
            <Select
              id="pipeline-chapter-concurrency"
              className={SETTINGS_CONTROL_CLASSNAME}
              disabled={disabled || glossaryMode !== "compiled" || precision}
              value={chapterConcurrency}
              onValueChange={(value) => onField("pipeline", "chapter_concurrency", Number(value))}
            >
              {concurrencyOptions.map((option) => (
                <SelectItem key={option} value={option}>
                  {option}
                </SelectItem>
              ))}
            </Select>
          }
        />
        <SettingsRow
          label={tr("settings.batchGlossary")}
          htmlFor="pipeline-batch-glossary"
          description={tr("settings.batchGlossaryHelp")}
          control={
            <Select
              id="pipeline-batch-glossary"
              className={SETTINGS_CONTROL_CLASSNAME}
              disabled={disabled}
              value={pipeline.batch_glossary === "relevant" ? "relevant" : "all"}
              onValueChange={(value) => onField("pipeline", "batch_glossary", value)}
            >
              <SelectItem value="all">{tr("settings.batchGlossaryAll")}</SelectItem>
              <SelectItem value="relevant">{tr("settings.batchGlossaryRelevant")}</SelectItem>
            </Select>
          }
        />
      </div>}
    </fieldset>
  );
}
