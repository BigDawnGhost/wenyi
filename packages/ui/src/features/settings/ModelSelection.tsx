import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Button } from "../../components/ui/button";
import { cardSurface } from "../../components/ui/card";
import { ErrorNotice } from "../../components/ui/data";
import { Select, SelectItem } from "../../components/ui/select";
import { useI18n } from "../../i18n";
import { operationLabel } from "../../i18n/labels";
import { cn } from "../../lib/utils";
import { object, splitModelRef, type Document } from "./registryEdits";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";

const thinkingOrder = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

function modelLabels(models: Document): Map<string, string> {
  return new Map(Object.keys(models).map(ref => {
    const { provider, model } = splitModelRef(ref);
    return [ref, `${provider} / ${model}`];
  }));
}

export function ModelSelection({
  llm,
  models,
  registeredModels = models,
  disabled,
  onChange,
  footer,
}: {
  llm: Document;
  models: Document;
  registeredModels?: Document;
  disabled: boolean;
  onChange: (llm: Document) => void;
  footer?: ReactNode;
}) {
  const { t } = useI18n();
  const capabilities = useQuery({ queryKey: ["capabilities"], queryFn: api.capabilities });
  const routes = object(llm.routes);
  const labels = modelLabels(models);
  const options = Array.from(labels, ([id, label]) => (
    <SelectItem key={id} value={id}>{label}</SelectItem>
  ));
  const thinkingChoices = (model: string): string[] => {
    const levels = object(registeredModels[model]).thinking_levels;
    if (!Array.isArray(levels)) return [];
    return [
      ...thinkingOrder.filter(level => levels.includes(level)),
      ...levels.filter(level => typeof level === "string" && !thinkingOrder.includes(level)),
    ];
  };
  const hasThinking = Object.values(routes).some(route => thinkingChoices(String(object(route).model ?? "")).length > 0);
  const changeRoute = (operation: string, route: Document) =>
    onChange({ ...llm, routes: { ...routes, [operation]: route } });
  const selectModel = (operation: string, model: string) => {
    const route: Document = { ...object(routes[operation]), model };
    // Promoting a fallback to primary must not leave it in the fallback list.
    if (Array.isArray(route.fallbacks)) {
      route.fallbacks = route.fallbacks.filter(id => id !== model);
    }
    if (typeof route.thinking === "string" && !thinkingChoices(model).includes(route.thinking)) {
      delete route.thinking;
    }
    changeRoute(operation, route);
  };
  return (
    <div className="min-w-0">
      {capabilities.isPending && <p role="status">{t("dashboard.loading")}</p>}
      {capabilities.error && <>
        <ErrorNotice error={capabilities.error} />
        <Button type="button" size="sm" variant="outline" disabled={capabilities.isFetching}
          onClick={() => void capabilities.refetch()}>{t("common.retry")}</Button>
      </>}
      <div className={cn(cardSurface, "overflow-hidden")}>
        {capabilities.data?.operations.map(({ id }) => {
          const route = object(routes[id]);
          const selection = String(route.model ?? "");
          const levels = thinkingChoices(selection);
          const thinking = String(route.thinking ?? object(registeredModels[selection]).thinking_value ?? "");
          const operation = operationLabel(id, t);
          return (
            <SettingsRow
              key={id}
              label={t("settings.operationModel", { operation })}
              htmlFor={`route-${id}`}
              controlClassName={hasThinking ? "max-w-none sm:w-112 sm:max-w-[65%]" : undefined}
              control={
                <>
                  <Select
                    id={`route-${id}`}
                    className={SETTINGS_CONTROL_CLASSNAME}
                    disabled={disabled}
                    value={selection}
                    valueLabel={labels.get(selection)}
                    onValueChange={value => selectModel(id, value)}
                  >
                    {options}
                  </Select>
                  {levels.length > 0 ? (
                    <Select
                      aria-label={`Thinking effort: ${operation}`}
                      className={cn(SETTINGS_CONTROL_CLASSNAME, "w-36 shrink-0")}
                      disabled={disabled}
                      value={thinking}
                      valueLabel={thinking || "—"}
                      onValueChange={value => changeRoute(id, { ...route, thinking: value })}
                    >
                      {levels.map(level => (
                        <SelectItem key={level} value={level}>{level}</SelectItem>
                      ))}
                    </Select>
                  ) : hasThinking && (
                    <span className="w-36 shrink-0 pl-3 text-sm text-muted-foreground">—</span>
                  )}
                </>
              }
            />
          );
        })}
        {footer && <div className="border-t border-border px-4 py-3">{footer}</div>}
      </div>
    </div>
  );
}
