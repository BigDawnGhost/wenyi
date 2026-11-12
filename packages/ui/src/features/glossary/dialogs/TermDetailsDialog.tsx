import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../../api/client";
import type { Term } from "../../../api/types";
import { useI18n } from "../../../i18n";
import { toast } from "../../../lib/toast";
import { Button } from "../../../components/ui/button";
import { Input, Textarea, Label } from "../../../components/ui/form";
import { Select, SelectItem } from "../../../components/ui/select";
import { ReadOnlyNotice } from "../../../components/ui/data";
import { Dialog } from "../../../components/ui/misc";
import { termTypes } from "../termTypes";

export function TermDetailsDialog({
  pid,
  term,
  showReading,
  readOnly,
  onClose,
  onSaved,
}: {
  pid: string;
  term: Term | null;
  showReading: boolean;
  readOnly: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t: tr } = useI18n();
  const [form, setForm] = useState({
    source: "",
    target: "",
    reading: "",
    type: "term",
    note: "",
    gender: "",
    aliases: "",
  });

  useEffect(() => {
    if (term) {
      setForm({
        source: term.source,
        target: term.target,
        reading: term.reading || "",
        type: term.type || "term",
        note: term.note || "",
        gender: term.gender || "",
        aliases: (term.aliases || []).join(", "),
      });
    }
  }, [term]);

  const m = useMutation({
    mutationFn: () =>
      api.updateTerm(pid, term!.source, {
        ...form,
        aliases: form.aliases
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      }),
    onSuccess: () => {
      toast.success(tr("glossary.updated"));
      onSaved();
    },
    onError: (e) =>
      toast.error(
        tr("glossary.couldNotUpdateTerm", { error: (e as Error).message }),
      ),
  });

  const resetMutation = m.reset;
  useEffect(() => {
    resetMutation();
  }, [term, resetMutation]);

  if (!term) return null;

  return (
    <Dialog
      open={!!term}
      onClose={onClose}
      title={tr(readOnly ? "glossary.termDetails" : "glossary.editTerm")}
    >
      {readOnly && <ReadOnlyNotice className="mb-3" />}
      {m.error && (
        <p role="alert" className="mb-3 text-sm text-destructive">
          {tr("glossary.couldNotUpdateTerm", { error: m.error.message })}
        </p>
      )}
      <div className="space-y-3">
        <div>
          <Label>{tr("glossary.requiredSourceTerm")}</Label>
          <Input
            value={form.source}
            readOnly={readOnly}
            onChange={(e) => setForm({ ...form, source: e.target.value })}
          />
        </div>
        <div>
          <Label>{tr("glossary.requiredTranslatedTerm")}</Label>
          <Input
            value={form.target}
            readOnly={readOnly}
            onChange={(e) => setForm({ ...form, target: e.target.value })}
          />
        </div>
        <div className={showReading ? "grid grid-cols-2 gap-3" : undefined}>
          {showReading && (
            <div>
              <Label>{tr("glossary.reading")}</Label>
              <Input
                aria-label={tr("glossary.reading")}
                value={form.reading}
                readOnly={readOnly}
                onChange={(e) => setForm({ ...form, reading: e.target.value })}
              />
            </div>
          )}
          <div>
            <Label>{tr("common.type")}</Label>
            <Select
              value={form.type}
              aria-label={tr("common.type")}
              disabled={readOnly}
              onValueChange={(value) => setForm({ ...form, type: value })}
            >
              {Object.entries(termTypes()).map(([id, label]) => (
                <SelectItem key={id} value={id}>
                  {label}
                </SelectItem>
              ))}
            </Select>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label>{tr("common.gender")}</Label>
            <Select
              value={form.gender}
              aria-label={tr("common.gender")}
              disabled={readOnly}
              onValueChange={(value) => setForm({ ...form, gender: value })}
            >
              <SelectItem value="">{tr("glossary.unspecified")}</SelectItem>
              <SelectItem value="male">{tr("glossary.male")}</SelectItem>
              <SelectItem value="female">{tr("glossary.female")}</SelectItem>
              <SelectItem value="other">{tr("glossary.other")}</SelectItem>
              <SelectItem value="unknown">{tr("glossary.unknown")}</SelectItem>
            </Select>
          </div>
        </div>
        <div>
          <Label>{tr("glossary.aliasesCommaSeparated")}</Label>
          <Input
            value={form.aliases}
            readOnly={readOnly}
            onChange={(e) => setForm({ ...form, aliases: e.target.value })}
            placeholder={tr("glossary.aliasPlaceholder")}
          />
        </div>
        <div>
          <Label>{tr("glossary.notes")}</Label>
          <Textarea
            value={form.note}
            readOnly={readOnly}
            onChange={(e) => setForm({ ...form, note: e.target.value })}
          />
        </div>
      </div>
      <div className="flex justify-end gap-2 mt-4">
        <Button variant="outline" onClick={onClose}>
          {tr(readOnly ? "common.close" : "common.cancel")}
        </Button>
        {!readOnly && (
          <Button
            onClick={() => m.mutate()}
            disabled={!form.source || !form.target || m.isPending}
          >
            {tr("common.save")}
          </Button>
        )}
      </div>
    </Dialog>
  );
}
