import { useEffect, useState, type ReactNode } from "react";
import { Input, Label } from "../../components/ui/form";
import { useI18n } from "../../i18n";
import type { RegistryGroup } from "./registryEdits";

/** Registry ID input; edits apply on Enter or blur, Escape restores the saved ID. */
export function RegistryIdField({
  group,
  id,
  ids,
  action,
  note,
  onRename,
  onPending,
}: {
  group: RegistryGroup;
  id: string;
  ids: string[];
  action?: ReactNode;
  note?: { id: string; text: string };
  onRename: (group: RegistryGroup, id: string, next: string) => void;
  onPending: (key: string, pending: boolean) => void;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState(id);
  const [error, setError] = useState("");
  const key = `${group}:${id}`;
  useEffect(() => () => onPending(key, false), [key, onPending]);
  const apply = () => {
    const next = draft.trim();
    if (!/^[a-zA-Z][a-zA-Z0-9_-]*$/.test(next)) {
      setError(t("registry.invalidId"));
      return;
    }
    if (next !== id && ids.includes(next)) {
      setError(t("registry.duplicateId"));
      return;
    }
    setError("");
    onPending(key, false);
    if (next !== id) onRename(group, id, next);
    else setDraft(id);
  };
  return (
    <div className="min-w-0 space-y-2">
      <Label htmlFor={`registry-${key}`}>
        {t(group === "providers" ? "registry.connectionId" : "registry.modelId")}
      </Label>
      <div className="flex gap-1">
        <Input
          id={`registry-${key}`}
          className="min-w-0"
          value={draft}
          aria-invalid={!!error}
          aria-describedby={error ? `error-${key}` : undefined}
          onChange={(event) => {
            setDraft(event.target.value);
            setError("");
            onPending(key, event.target.value !== id);
          }}
          onBlur={apply}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              apply();
            } else if (event.key === "Escape") {
              setDraft(id);
              setError("");
              onPending(key, false);
            }
          }}
        />
        {action}
      </div>
      {error && (
        <p id={`error-${key}`} role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {note && (
        <p
          id={note.id}
          className="text-xs text-muted-foreground [overflow-wrap:anywhere]"
        >
          {note.text}
        </p>
      )}
    </div>
  );
}
