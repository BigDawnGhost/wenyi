import { useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import * as Popper from "@radix-ui/react-popper";
import { Check, ChevronDown, RefreshCw } from "lucide-react";
import { api } from "../../api/client";
import type { ModelListResult } from "../../api/types";
import { useI18n, type MessageKey } from "../../i18n";
import { Input } from "../../components/ui/form";
import { Button } from "../../components/ui/button";
import { cn } from "../../lib/utils";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";

type Document = Record<string, unknown>;

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
  credential,
  action,
  onChange,
}: {
  id: string;
  value: string;
  connection: string | null;
  provider: Document | undefined;
  credential?: string;
  action: ReactNode;
  onChange: (value: string) => void;
}) {
  const { t } = useI18n();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [filter, setFilter] = useState("");
  const [pending, setPending] = useState(false);
  const [reply, setReply] = useState<{ signature: symbol; result?: ModelListResult }>();
  // Invalidate a listing when its connection or account changes, without
  // serializing a key or attaching it to the result.
  const signature = useMemo(() => Symbol(), [connection, provider, credential]);
  const latest = useRef(signature);
  useLayoutEffect(() => { latest.current = signature; }, [signature]);
  const fetchNames = async () => {
    if (!provider || pending) return;
    const focused = document.activeElement;
    setPending(true);
    try {
      // Keep the key and the closure that reads it out of the mutation cache.
      const result = await api.listModels({
        connection,
        provider,
        api_key: credential?.trim() ?? null,
      });
      setReply({ signature, result });
      if (result.ok && result.models.length && latest.current === signature &&
        (document.activeElement === focused || document.activeElement === inputRef.current)) {
        setFilter("");
        setActive(-1);
        setOpen(true);
        inputRef.current?.focus();
      }
    } catch {
      setReply({ signature });
    } finally {
      setPending(false);
    }
  };
  const current = reply?.signature === signature;
  const result = current ? reply.result : undefined;
  const listed = result?.ok ? result.models : null;
  const shown = useMemo(() => {
    if (!listed) return [];
    const query = filter.trim().toLowerCase();
    return query ? listed.filter((name) => name.toLowerCase().includes(query)) : listed;
  }, [listed, filter]);
  const failure = result && !result.ok
    ? t(failureMessages[result.category] ?? "modelList.failed")
    : current && !result
      ? t("modelList.failed")
      : null;
  const expanded = open && shown.length > 0;
  const dialog = inputRef.current?.closest<HTMLElement>('[role="dialog"]');
  useLayoutEffect(() => {
    if (expanded && active >= 0)
      document.getElementById(`${listId}-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [expanded, active, listId]);
  const choose = (name: string) => {
    onChange(name);
    setOpen(false);
    setFilter("");
  };
  return (
    <SettingsRow label={t("common.modelName")} htmlFor={id} description={t("providerSettings.remoteHelp")}
      control={<div className="w-full min-w-0 space-y-1">
      <div className="flex gap-1">
        <div className="relative min-w-0 flex-1">
          <Input
            ref={inputRef}
            id={id}
            role="combobox"
            aria-expanded={expanded}
            aria-controls={expanded ? listId : undefined}
            aria-autocomplete="list"
            aria-activedescendant={expanded && active >= 0 ? `${listId}-${active}` : undefined}
            autoComplete="off"
            spellCheck={false}
            className={cn(SETTINGS_CONTROL_CLASSNAME, listed && "pr-8")}
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
                event.stopPropagation();
                setOpen(false);
              }
            }}
          />
          {listed && (
            <button
              type="button"
              tabIndex={-1}
              aria-hidden="true"
              className="absolute right-0 top-0 flex h-8 w-8 items-center justify-center text-muted-foreground"
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
          {expanded && dialog && (
            <Popper.Root>
              <Popper.Anchor virtualRef={inputRef} />
              {createPortal(
                <Popper.Content asChild align="start" sideOffset={4}
                  collisionBoundary={dialog} collisionPadding={8} hideWhenDetached
                >
                  <ul
                    id={listId}
                    role="listbox"
                    aria-label={t("common.modelName")}
                    className="z-50 max-h-[min(theme(spacing.64),var(--radix-popper-available-height))] w-[var(--radix-popper-anchor-width)] overflow-y-auto rounded-xl bg-popover p-1 text-sm text-popover-foreground shadow-e4"
                  >
                    {shown.map((name, index) => (
                      <li
                        key={name}
                        id={`${listId}-${index}`}
                        role="option"
                        aria-selected={name === value}
                        className={cn(
                          "flex cursor-pointer items-center justify-between gap-2 rounded-lg px-2 py-1.5 [overflow-wrap:anywhere]",
                          index === active && "bg-accent text-accent-foreground",
                        )}
                        onMouseDown={(event) => { event.preventDefault(); choose(name); }}
                        onMouseEnter={() => setActive(index)}
                      >
                        {name}
                        {name === value && <Check className="h-4 w-4 shrink-0" aria-hidden="true" />}
                      </li>
                    ))}
                  </ul>
                </Popper.Content>,
                dialog,
              )}
            </Popper.Root>
          )}
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="shrink-0 text-muted-foreground"
          // Only genuinely unavailable disables the button. Blocking the second
          // press with `disabled` also applied the disabled face mid-request, and
          // its pointer-events cut dropped the hover state under the cursor.
          disabled={!provider || (credential !== undefined && !credential.trim())}
          aria-busy={pending || undefined}
          aria-label={t("modelList.fetch")}
          title={t("modelList.fetch")}
          onClick={() => void fetchNames()}
        >
          {/* The icon stays put while a fetch runs: swapping it for a spinner
              replaced the glyph and added a rotation the control never has. */}
          <RefreshCw className="h-4 w-4" aria-hidden="true" />
        </Button>
        {action}
      </div>
      {failure && <p role="alert" className="text-xs text-destructive">{failure}</p>}
      <p role="status" className="text-xs">
        {listed ? (
          <span className="text-muted-foreground">
            {listed.length
              ? t("modelList.count", { count: listed.length })
              : t("modelList.empty")}
          </span>
        ) : null}
      </p>
    </div>} />
  );
}
