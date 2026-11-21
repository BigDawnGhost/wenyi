import { useRef, useState } from "react";
import { Plus } from "lucide-react";
import type { Capabilities, GlobalConfig } from "../../api/types";
import { Button } from "../../components/ui/button";
import { Card } from "../../components/ui/card";
import { EditButton } from "../../components/ui/edit-button";
import { useI18n } from "../../i18n";
import { ProviderDialog, type ProviderChanges } from "./ProviderDialog";
import { object, type Document, nextId } from "./registryEdits";
import { SettingsGroupHeader } from "./SettingsSection";

export function ProviderSettings({
  llm, draftLlm, revision, disabled, capabilities, onSave,
}: {
  llm: Document;
  draftLlm: Document;
  revision: number;
  disabled: boolean;
  capabilities?: Capabilities;
  onSave: (changes: ProviderChanges) => Promise<GlobalConfig>;
}) {
  const { t } = useI18n();
  const [editor, setEditor] = useState<{ id: string; creating: boolean } | null>(null);
  const addButton = useRef<HTMLButtonElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const open = (button: HTMLButtonElement, id: string, creating: boolean) => {
    opener.current = button;
    setEditor({ id, creating });
  };
  const providers = object(llm.providers);
  return (
    <div className="space-y-3">
      <SettingsGroupHeader title={t("providerSettings.models")} action={
        <Button ref={addButton} variant="outline" size="sm" disabled={disabled} onClick={event =>
          open(event.currentTarget, nextId("provider", Object.keys(providers)), true)}>
          <Plus className="h-4 w-4" aria-hidden="true" />{t("providerSettings.addApiConnection")}
        </Button>
      } />
      <Card className="divide-y divide-border">
        {Object.entries(providers).map(([id, raw]) => {
          const kind = String(object(raw).kind);
          return (
            <div key={id} className="space-y-2 px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <h4 className="min-w-0 break-words text-sm font-medium">{id}
                  {kind !== id && <span className="ml-2 text-xs font-normal text-muted-foreground">{kind}</span>}
                </h4>
                <EditButton
                  label={t("providerSettings.editProvider", { id })}
                  disabled={disabled}
                  onClick={event => open(event.currentTarget, id, false)}
                />
              </div>
            </div>
          );
        })}
      </Card>
      {editor && <ProviderDialog initial={{ ...editor, llm, revision }} draftLlm={draftLlm}
        returnFocus={() => opener.current?.isConnected ? opener.current : addButton.current}
        capabilities={capabilities}
        onClose={() => setEditor(null)}
        onSave={onSave} />}
    </div>
  );
}
