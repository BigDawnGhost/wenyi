import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useI18n } from "@wenyi/ui/i18n";
import { Button, Select, SelectItem } from "@wenyi/ui/primitives";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "@wenyi/ui/settings";
import { toast } from "@wenyi/ui/toast";
import { useDesktopI18n } from "./i18n";

const defaultZoom = 100;
const presets = [80, 90, defaultZoom, 110, 125] as const;
type ZoomPercent = typeof presets[number];
const zoomKey = ["desktopInterfaceZoom"] as const;

async function interfaceZoom(percent?: ZoomPercent): Promise<ZoomPercent | null> {
  const native = (window as unknown as {
    __TAURI_INTERNALS__?: {
      invoke: (command: string, args?: { percent: ZoomPercent }) => Promise<unknown>;
    };
  }).__TAURI_INTERNALS__;
  if (!native) throw new Error("Native interface zoom is unavailable");
  const value = await native.invoke("desktop_interface_zoom", percent === undefined ? undefined : { percent });
  if (value === null) return null;
  const preset = presets.find(option => option === value);
  if (preset !== undefined) return preset;
  throw new Error("Invalid native interface zoom response");
}

function useApplyZoom(reportError?: () => void) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (percent: ZoomPercent) => interfaceZoom(percent),
    networkMode: "always",
    // The shortcut and settings row must apply changes in the order requested.
    scope: { id: zoomKey[0] },
    onError: () => {
      reportError?.();
      return client.invalidateQueries({ queryKey: zoomKey });
    },
    onSuccess: async (percent) => {
      await client.cancelQueries({ queryKey: zoomKey });
      client.setQueryData(zoomKey, percent);
    },
  });
}

export function useInterfaceZoomReset() {
  const { t } = useDesktopI18n();
  const { mutate } = useApplyZoom(() => toast.error(t("zoom.failed"), { id: "desktop-zoom-reset" }));
  useEffect(() => {
    function reset(event: KeyboardEvent) {
      if (event.key !== "0" || event.ctrlKey === event.metaKey ||
          event.altKey || event.shiftKey || event.repeat || event.isComposing) return;
      event.preventDefault();
      mutate(defaultZoom);
    }
    // Reset must work even when the settings route is not mounted or an overlay has focus.
    window.addEventListener("keydown", reset, true);
    return () => window.removeEventListener("keydown", reset, true);
  }, [mutate]);
}

export function InterfaceZoomSettings() {
  const { t } = useDesktopI18n();
  const { t: common } = useI18n();
  const preference = useQuery({
    queryKey: zoomKey,
    queryFn: () => interfaceZoom(),
    networkMode: "always",
    retry: false,
    staleTime: Infinity,
  });
  const change = useApplyZoom();
  const current = preference.data;
  if (current === null) return null;

  // Keep the whole selector on the visible side when page zoom narrows the viewport.
  return (
    <SettingsRow
      label={t("zoom.setting")}
      htmlFor="interface-zoom"
      description={<span className="block max-w-80">{t("zoom.help")}</span>}
      controlClassName="max-w-none basis-full justify-start lg:basis-auto lg:justify-end"
      control={
        <div className="w-full max-w-80">
          <Select
            id="interface-zoom"
            className={SETTINGS_CONTROL_CLASSNAME}
            disabled={current === undefined}
            value={current === undefined ? "" : String(current)}
            valueLabel={current === undefined ? "—" : undefined}
            onValueChange={(value) => {
              const percent = presets.find(option => String(option) === value);
              if (percent !== undefined) change.mutate(percent);
            }}
          >
            {presets.map(percent => (
              <SelectItem key={percent} value={String(percent)}>{percent}%</SelectItem>
            ))}
          </Select>
        </div>
      }
      detail={preference.isError ? <div className="flex items-center gap-3">
        <p role="alert" className="text-sm text-destructive">{t("zoom.readFailed")}</p>
        <Button variant="outline" size="sm" disabled={preference.isFetching}
          onClick={() => void preference.refetch()}>{common("common.retry")}</Button>
      </div> : change.isError ? <p role="alert" className="max-w-80 text-sm text-destructive">
        {t("zoom.failed")}
      </p> : undefined}
    />
  );
}
