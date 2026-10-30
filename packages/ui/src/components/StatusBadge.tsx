import { useI18n } from "@/i18n";
import { statusLabel, statusTone, type StatusContext } from "@/i18n/status";
import { Badge } from "./ui/badge";

export function StatusBadge({
  status,
  context,
}: {
  status: string;
  context?: StatusContext;
}) {
  const { t } = useI18n();
  return (
    <Badge variant={statusTone(status)}>
      {statusLabel(status, t, context)}
    </Badge>
  );
}
