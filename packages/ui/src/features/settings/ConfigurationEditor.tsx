import { useRef, useState } from "react";
import { useIsMutating, useMutation } from "@tanstack/react-query";
import { Button } from "../../components/ui/button";
import { ErrorNotice } from "../../components/ui/data";
import { Textarea } from "../../components/ui/form";
import { useI18n, type MessageKey } from "../../i18n";
import { errorMessage } from "../../lib/error-codes";
import { toast } from "../../lib/toast";

type Document = Record<string, unknown>;
type Configuration = { effective: Document; yaml: string };
type Write<T> = { yaml: string; version: { value: T }; id: number; fromYaml: boolean };
const empty: Document = {};

export function useConfigurationEditor<T extends Configuration>({
  scope, saved, write, onSaved, successMessage,
}: {
  scope: string;
  saved: T | undefined;
  write: (yaml: string, base: T) => Promise<T>;
  onSaved: (value: T) => void;
  successMessage: MessageKey;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState<Document>();
  const [yamlText, setYamlText] = useState<string>();
  const base = useRef<{ value: T } | undefined>(undefined);
  const latest = useRef(0);
  const mutationKey = ["configurationWrite", scope];
  const pendingWrites = useIsMutating({ mutationKey, exact: true });
  const save = useMutation({
    mutationKey,
    scope: { id: scope },
    mutationFn: ({ yaml, version }: Write<T>) => write(yaml, version.value),
    onSuccess: (value, request) => {
      // The next queued write must use the revision returned by this write, not
      // a background refetch that the draft was never based on.
      request.version.value = value;
      onSaved(value);
      if (request.id !== latest.current) return;
      base.current = undefined;
      setDraft(undefined);
      setYamlText(undefined);
      toast.success(t(successMessage));
    },
    onError: (error, request) => {
      // An ordinary control can be far below the inline retry notice.
      if (request.id === latest.current && !request.fromYaml)
        toast.error(errorMessage(error, t));
    },
  });
  const pending = save.isPending || pendingWrites > 0;
  const submit = (yaml: string, fromYaml: boolean) => {
    if (!saved || (pending && !save.isPending)) return;
    base.current ??= { value: saved };
    save.mutate({ yaml, version: base.current, id: ++latest.current, fromYaml });
  };
  const yamlDirty = yamlText !== undefined;
  const yaml = yamlText ?? (draft ? JSON.stringify(draft, null, 2) : saved?.yaml ?? "");

  return {
    effective: draft ?? saved?.effective ?? empty,
    yaml,
    yamlDirty,
    dirty: yamlDirty || draft !== undefined,
    pending,
    formBlocked: yamlDirty || (pending && !save.isPending),
    error: save.error,
    change(next: Document) {
      setDraft(next);
      submit(JSON.stringify(next, null, 2), false);
    },
    editYaml(next: string) {
      if (pending || !saved) return;
      // Pin the same revision for the lifetime of a YAML draft, even if the
      // query refreshes before the user applies it.
      if (next === saved.yaml) {
        base.current = undefined;
        setYamlText(undefined);
      } else {
        base.current ??= { value: saved };
        setYamlText(next);
      }
      save.reset();
    },
    submit: () => { if (!pending) submit(yaml, yamlDirty); },
    discard: () => {
      if (pending) return;
      base.current = undefined;
      setDraft(undefined);
      setYamlText(undefined);
      save.reset();
    },
  };
}

type Editor = ReturnType<typeof useConfigurationEditor>;

export function ConfigurationSaveStatus({ editor, disabled }: { editor: Editor; disabled: boolean }) {
  const { t } = useI18n();
  if (editor.pending) return <p role="status" className="sr-only">{t("common.saving")}</p>;
  if (!editor.error || editor.yamlDirty) return null;
  return (
    <div className="space-y-2">
      <ErrorNotice error={editor.error} />
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={editor.submit} disabled={disabled}>{t("common.retry")}</Button>
        <Button size="sm" variant="outline" onClick={editor.discard}>{t("settings.discardChanges")}</Button>
      </div>
    </div>
  );
}

export function ConfigurationYaml({ editor, disabled }: { editor: Editor; disabled: boolean }) {
  const { t } = useI18n();
  const textarea = useRef<HTMLTextAreaElement>(null);
  return (
    <div className="space-y-3">
      <Textarea
        ref={textarea}
        aria-label={t("settings.advancedYamlConfiguration")}
        spellCheck={false}
        className="min-h-editor font-mono text-xs"
        value={editor.yaml}
        disabled={disabled}
        readOnly={editor.pending || (editor.dirty && !editor.yamlDirty)}
        onChange={(event) => editor.editYaml(event.target.value)}
      />
      {editor.yamlDirty && (
        <>
          <ErrorNotice error={editor.error} />
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={disabled || editor.pending} onClick={() => {
              textarea.current?.focus();
              editor.submit();
            }}>
              {t("settings.applyYaml")}
            </Button>
            <Button size="sm" variant="outline" disabled={editor.pending} onClick={() => {
              textarea.current?.focus();
              editor.discard();
            }}>
              {t("settings.discardChanges")}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
