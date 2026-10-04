import { platform } from "../platform";

export const progressInterval = (connected: boolean, fallback: number) =>
  platform().progressInterval(connected, fallback);
