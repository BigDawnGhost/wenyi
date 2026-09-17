import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type GlobalConfig } from "@/lib/api";
import { useI18n } from "@/i18n";
import { workflowTemplateLabel } from "@/i18n/labels";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorNotice } from "@/components/ui/data";
import { Disclosure } from "@/components/ui/disclosure";
import { Label, Select, Textarea } from "@/components/ui/form";
import { ModelSelection } from "./ModelSelection";
import { ProviderSettings } from "./ProviderSettings";
import { WorkflowSettings } from "./WorkflowSettings";
import { renameRegistryId, type RegistryGroup } from "./registryEdits";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;

export function GlobalConfiguration() {
  const { t } = useI18n();
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["globalConfig"],
    queryFn: api.getGlobalConfig,
  });
  const { data: caps } = useQuery({
    queryKey: ["capabilities"],
    queryFn: api.capabilities,
  });
  const { data: templates } = useQuery({
    queryKey: ["templates"],
    queryFn: api.listTemplates,
  });
  const [draft, setDraft] = useState<GlobalConfig | null>(null);
  const [yamlDirty, setYamlDirty] = useState(false);
  const [registryKey, setRegistryKey] = useState(0);
  const [editingIds, setEditingIds] = useState(false);
  const [renames, setRenames] = useState<Record<string, string>>({});
  useEffect(() => {
    if (query.data && !draft) setDraft(query.data);
  }, [query.data, draft]);
  const input = () => ({
    yaml: draft!.yaml,
    revision: draft!.revision,
    default_template: draft!.default_template,
    model_renames: renames,
  });
  const apply = (value: GlobalConfig) => {
    setDraft(value);
    setYamlDirty(false);
  };
  const save = useMutation({
    mutationFn: () => api.saveGlobalConfig(input()),
    onSuccess: (value) => {
      apply(value);
      setRenames({});
      qc.setQueryData(["globalConfig"], value);
      for (const key of ["config", "models", "templates"])
        void qc.invalidateQueries({ queryKey: [key] });
      toast.success(t("settings.globalSaved"));
    },
  });
  const validate = useMutation({
    mutationFn: () => api.validateGlobalConfig(input()),
    onSuccess: (value) => {
      apply(value);
      toast.success(t("settings.configurationIsValidButNotSavedYet"));
    },
  });
  const restore = useMutation({
    mutationFn: api.getGlobalDefaults,
    onSuccess: (value) => {
      apply({ ...value, revision: draft!.revision });
      setRenames({});
      setRegistryKey((value) => value + 1);
      toast.success(t("settings.defaultsLoaded"));
    },
  });
  const error = save.error || validate.error || restore.error;
  const pending = save.isPending || validate.isPending || restore.isPending;
  const disabled = !draft || yamlDirty || pending;
  const effective = draft?.effective || {};
  const llm = object(effective.llm);
  const change = (next: Document) => {
    if (draft)
      setDraft({
        ...draft,
        effective: next,
        yaml: JSON.stringify(next, null, 2),
      });
  };
  const changeLlm = (value: Document) => {
    change({ ...effective, llm: { ...value, preset: null } });
    setRenames((current) =>
      Object.fromEntries(
        Object.entries(current).filter(([, id]) => id in object(value.models)),
      ),
    );
  };
  const rename = (group: RegistryGroup, oldId: string, newId: string) => {
    if (group === "models") {
      const next = { ...renames };
      const original = Object.keys(next).find((id) => next[id] === oldId);
      if (original) {
        if (original === newId) delete next[original];
        else next[original] = newId;
      } else if (
        oldId in object(object(query.data?.effective.llm).models) &&
        !(oldId in next)
      )
        next[oldId] = newId;
      setRenames(next);
    }
    changeLlm(renameRegistryId(llm, group, oldId, newId));
  };
  return (
    <>
      <ErrorNotice error={query.error || error} />
      <Card>
        <CardContent className="p-5 space-y-4">
          <ProviderSettings
            key={registryKey}
            config={effective}
            kinds={caps?.providers || []}
            disabled={disabled}
            error={error}
            onChange={changeLlm}
            onRename={rename}
            onEditingChange={setEditingIds}
          />
        </CardContent>
      </Card>
      <Card>
        <CardContent className="p-5 space-y-4">
          <h2 className="font-medium">{t("settings.newProjectDefaults")}</h2>
          <p className="text-sm text-muted-foreground">
            {t("settings.defaultScopeHelp")}
          </p>
          <ModelSelection
            llm={llm}
            models={object(llm.models)}
            operations={caps?.operations}
            disabled={disabled}
            onChange={changeLlm}
          />
          <div>
            <Label htmlFor="default-template">
              {t("settings.defaultTemplate")}
            </Label>
            <Select
              id="default-template"
              disabled={disabled}
              value={draft?.default_template || ""}
              onChange={(event) => {
                if (draft)
                  setDraft({ ...draft, default_template: event.target.value });
              }}
            >
              {templates?.map((template) => (
                <option key={template.name} value={template.name}>
                  {workflowTemplateLabel(
                    template.name,
                    template.description,
                    t,
                  )}
                </option>
              ))}
            </Select>
          </div>
          <h3 className="text-sm font-medium">
            {t("settings.standardDefaults")}
          </h3>
          <p className="text-sm text-muted-foreground">
            {t("settings.quickTemplateHelp")}
          </p>
          <WorkflowSettings
            config={effective}
            disabled={disabled}
            pdf
            error={error}
            onField={(group, key, value) =>
              change({
                ...effective,
                [group]: { ...object(effective[group]), [key]: value },
              })
            }
          />
        </CardContent>
      </Card>
      <Card>
        <CardContent className="p-5 space-y-4">
          {yamlDirty && (
            <p className="text-sm text-muted-foreground">
              {t("settings.advancedYamlHasUnvalidatedChangesValidateIt")}
            </p>
          )}
          <Disclosure
            title={t("settings.advancedYamlConfiguration")}
            summary={t(
              yamlDirty ? "settings.unsavedSummary" : "settings.yamlSummary",
            )}
            error={error}
          >
            <p className="text-sm text-muted-foreground">
              {t("settings.useServerEnvironmentVariableNamesForApi")}
            </p>
            <Textarea
              aria-label={t("settings.advancedYamlConfiguration")}
              spellCheck={false}
              className="min-h-[420px] font-mono text-xs"
              value={draft?.yaml || ""}
              disabled={!draft || pending}
              onChange={(event) => {
                if (draft) {
                  setDraft({ ...draft, yaml: event.target.value });
                  setYamlDirty(true);
                }
              }}
            />
          </Disclosure>
          {editingIds && (
            <p className="text-sm text-muted-foreground">
              {t("registry.finishRenaming")}
            </p>
          )}
          <div className="flex flex-wrap gap-3">
            <Button
              variant="outline"
              disabled={!draft || pending}
              onClick={() => restore.mutate()}
            >
              {t("settings.restoreDefaults")}
            </Button>
            <Button
              variant="outline"
              disabled={!draft || pending || editingIds}
              onClick={() => validate.mutate()}
            >
              {t("settings.validateConfiguration")}
            </Button>
            <Button
              disabled={!draft || pending || editingIds}
              onClick={() => save.mutate()}
            >
              {pending ? t("common.saving") : t("settings.saveConfiguration")}
            </Button>
          </div>
        </CardContent>
      </Card>
    </>
  );
}
