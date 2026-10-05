import { useSyncExternalStore } from "react";
import { platform } from "../platform";

export const progressInterval = (connected: boolean, fallback: number) =>
  platform().progressInterval(connected, fallback);

export function useForeground() {
  const { subscribe, isForeground } = platform().activity;
  return useSyncExternalStore(subscribe, isForeground);
}
