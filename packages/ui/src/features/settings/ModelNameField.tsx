import { useId, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Check, ChevronDown, Loader2, RefreshCw } from "lucide-react";
import { api, type ModelListResult } from "@/lib/api";
import { useI18n, type MessageKey } from "@/i18n";
import { Input, Label } from "@/components/ui/form";
import { Button } from "@/components/ui/button";
import { platform } from "@/platform";
import { cn } from "@/lib/utils";

type Document = Record<string, unknown>;
type ListingDraft = {
  connection: string;
  provider: Document | undefined;
  signature: string;
};

// Provider-independent failure categories from wenyi_core.llm.errors and models.py.
const failureMessages: Record<string, MessageKey> = {
  missing_credential: "connectivity.missingCredential",
  authentication_failed: "connectivity.authenticationFailed",
  insufficient_balance: "connectivity.insufficientBalance",
  permission_denied: "connectivity.permissionDenied",
  model_or_endpoint_not_found: "modelList.notFound",
  rate_limited: "connectivity.rateLimited",
  timeout: "connectivity.timeout",
  connection_failed: "connectivity.connectionFailed",
  provider_unavailable: "connectivity.unavailable",
  unsupported: "modelList.unsupported",
};

/**
 * Model name combobox: free text always works; "Fetch models" asks the selected
 * connection for its list, which then filters as the user types.
 */
export function ModelNameField({
  id,
  value,
  connection,
  provider,
  onChange,
}: {
  id: string;
  value: string;
  connection: string;
  provider: Document | undefined;
  onChange: (value: string) => void;
}) {
  const { t } = useI18n();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [filter, setFilter] = useState("");
  // A listing belongs to one connection draft; editing it makes the list stale.
  const signature = JSON.stringify([connection, provider]);
  const fetch = useMutation({
    mutationFn: ({ connection, provider }: ListingDraft): Promise<ModelListResult> =>
      api.listModels({
        connection,
        provider: provider ?? {},
        api_key: platform().capabilities.credentials?.forListing(connection) ?? null,
      }),
    onSuccess: (result, draft) => {
      if (draft.signature === signature && result.ok && result.models.length) {
        setFilter("");
        setActive(-1);
        setOpen(true);
        inputRef.current?.focus();
      }
    },
  });
  const current = fetch.variables?.signature === signature;
  const result = current ? fetch.data : undefined;
  const listed = result?.ok ? result.models : null;
  const shown = useMemo(() => {
    if (!listed) return [];
    const query = filter.trim().toLowerCase();
    return query ? listed.filter((name) => name.toLowerCase().includes(query)) : listed;
  }, [listed, filter]);
  const failure = result && !result.ok
    ? t(failureMessages[result.category] ?? "modelList.failed")
    : current && fetch.error
      ? t("modelList.failed")
      : null;
  const expanded = open && shown.length > 0;
  const choose = (name: string) => {
    onChange(name);
    setOpen(false);
    setFilter("");
  };
  return (
    <div className="min-w-0 space-y-2">
      <Label htmlFor={id}>{t("common.modelName")}</Label>
      <div className="flex gap-1">
        <div className="relative min-w-0 flex-1">
          <Input
            ref={inputRef}
            id={id}
            role="combobox"
            aria-expanded={expanded}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={expanded && active >= 0 ? `${listId}-${active}` : undefined}
            autoComplete="off"
            spellCheck={false}
            className={cn(listed && "pr-8")}
            value={value}
            onChange={(event) => {
              onChange(event.target.value);
              setFilter(event.target.value);
              setActive(-1);
              setOpen(true);
            }}
            onFocus={() => listed && setOpen(true)}
            onBlur={() => setOpen(false)}
            onKeyDown={(event) => {
              if (!listed) return;
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                setOpen(true);
                const step = event.key === "ArrowDown" ? 1 : -1;
                setActive((index) => (index + step + shown.length) % Math.max(shown.length, 1));
              } else if (event.key === "Enter" && expanded && active >= 0) {
                event.preventDefault();
                choose(shown[active]);
              } else if (event.key === "Escape" && open) {
                event.preventDefault();
                setOpen(false);
              }
            }}
          />
          {listed && (
            <button
              type="button"
              tabIndex={-1}
              aria-hidden="true"
              className="absolute inset-y-0 right-0 flex w-8 items-center justify-center text-muted-foreground"
              onMouseDown={(event) => {
                // Keep focus in the input while toggling the list.
                event.preventDefault();
                setFilter("");
                setOpen((current) => !current);
                inputRef.current?.focus();
              }}
            >
              <ChevronDown className="h-4 w-4" />
            </button>
          )}
          {expanded && (
            <ul
              id={listId}
              role="listbox"
              aria-label={t("common.modelName")}
              className="absolute left-0 right-0 top-full z-50 mt-1 max-h-64 overflow-y-auto rounded-md border bg-background p-1 text-sm shadow-lg"
            >
              {shown.map((name, index) => (
                <li
                  key={name}
                  id={`${listId}-${index}`}
                  role="option"
                  aria-selected={name === value}
                  className={cn(
                    "flex cursor-pointer items-center justify-between gap-2 rounded px-2 py-1.5 [overflow-wrap:anywhere]",
                    index === active && "bg-accent text-accent-foreground",
                  )}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    choose(name);
                  }}
                  onMouseEnter={() => setActive(index)}
                >
                  {name}
                  {name === value && <Check className="h-4 w-4 shrink-0" aria-hidden="true" />}
                </li>
              ))}
            </ul>
          )}
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="shrink-0 text-muted-foreground"
          disabled={!provider || fetch.isPending}
          aria-label={t("modelList.fetch")}
          title={t("modelList.fetch")}
          onClick={() => fetch.mutate({ connection, provider, signature })}
        >
          {fetch.isPending
            ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            : <RefreshCw className="h-4 w-4" aria-hidden="true" />}
        </Button>
      </div>
      <p role="status" aria-live="polite" className="text-xs">
        {failure ? (
          <span className="text-destructive">{failure}</span>
        ) : listed ? (
          <span className="text-muted-foreground">
            {listed.length
              ? t("modelList.count", { count: listed.length })
              : t("modelList.empty")}
          </span>
        ) : null}
      </p>
    </div>
  );
}
