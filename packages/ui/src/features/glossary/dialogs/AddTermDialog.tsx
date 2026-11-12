import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { useI18n } from "../../../i18n";
import { toast } from "../../../lib/toast";
import { Button } from "../../../components/ui/button";
import { Input, Textarea, Label } from "../../../components/ui/form";
import { Select, SelectItem } from "../../../components/ui/select";
import { Dialog } from "../../../components/ui/misc";
import { termTypes } from "../termTypes";

export function AddTermDialog({
  pid,
  open,
  showReading,
  onClose,
  onSaved,
}: {
  pid: string;
  open: boolean;
  showReading: boolean;
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
  });
  const m = useMutation({
    mutationFn: () => api.addTerm(pid, form),
    onSuccess: () => {
      toast.success(tr("glossary.added"));
      setForm({ source: "", target: "", reading: "", type: "term", note: "" });
      onSaved();
    },
    onError: (e) =>
      toast.error(
        tr("glossary.couldNotAddTerm", { error: (e as Error).message }),
      ),
  });
  const resetMutation = m.reset;
  useEffect(() => {
    if (open) resetMutation();
  }, [open, resetMutation]);
  return (
    <Dialog open={open} onClose={onClose} title={tr("glossary.addTerm")}>
      {m.error && (
        <p role="alert" className="mb-3 text-sm text-destructive">
          {tr("glossary.couldNotAddTerm", { error: m.error.message })}
        </p>
      )}
      <div className="space-y-3">
        <div>
          <Label>{tr("glossary.requiredSourceTerm")}</Label>
          <Input
            value={form.source}
            onChange={(e) => setForm({ ...form, source: e.target.value })}
          />
        </div>
        <div>
          <Label>{tr("glossary.requiredTranslatedTerm")}</Label>
          <Input
            value={form.target}
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
                onChange={(e) => setForm({ ...form, reading: e.target.value })}
              />
            </div>
          )}
          <div>
            <Label>{tr("common.type")}</Label>
            <Select
              value={form.type}
              aria-label={tr("common.type")}
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
        <div>
          <Label>{tr("glossary.notes")}</Label>
          <Textarea
            value={form.note}
            onChange={(e) => setForm({ ...form, note: e.target.value })}
          />
        </div>
      </div>
      <div className="flex justify-end gap-2 mt-4">
        <Button variant="outline" onClick={onClose}>
          {tr("common.cancel")}
        </Button>
        <Button
          onClick={() => m.mutate()}
          disabled={!form.source || !form.target || m.isPending}
        >
          {tr("glossary.add")}
        </Button>
      </div>
    </Dialog>
  );
}
