import type { PlatformServices, ProjectSource } from "./contracts";

let services: PlatformServices | undefined;
export function configurePlatform(value: PlatformServices) {
  if (services) throw new Error("Platform services are already configured.");
  services = value;
}
export function platform(): PlatformServices {
  if (!services) throw new Error("The application host must configure platform services.");
  return services;
}

export function releaseSource(source: ProjectSource | null) {
  if (source && "release" in source) source.release();
}
