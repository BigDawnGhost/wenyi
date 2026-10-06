import type { ComponentType, ReactNode } from "react";
import type { components } from "@wenyi/shared-schema";
import type { ProjectDetail } from "./lib/api";

export type ProjectInput = Pick<components["schemas"]["ProjectCreate"], "name"> &
  Partial<components["schemas"]["ProjectCreate"]>;
export type ExportOptions = Partial<components["schemas"]["ExportRequest"]>;

/** An opaque selected-source capability, not a filesystem path. */
export interface SourceCapability {
  name: string;
  size: number;
  upload: (project: ProjectInput) => Promise<ProjectDetail>;
  release: () => void;
}
export type ProjectSource = File | SourceCapability;
export function releaseSource(source: ProjectSource | null) {
  if (source && "release" in source) source.release();
}

export interface SourceDropCallbacks {
  disabled: boolean;
  select: (source: ProjectSource) => boolean;
  dragging: (value: boolean) => void;
  error: (message: string) => void;
}

export interface CredentialFieldProps {
  connection: string;
  saved: boolean;
  children?: ReactNode;
}

/** Selected once by the host before mounting React; no implicit browser fallback. */
export interface PlatformServices {
  /** Hosts that suspend background updates reconcile durable state on return. */
  activity: {
    isForeground: () => boolean;
    subscribe: (listener: () => void) => () => void;
  };
  preferences: {
    get: (
      key: "wenyi.locale" | "wenyi.sidebarCollapsed" | "wenyi.lastProject",
    ) => string | null;
    set: (
      key: "wenyi.locale" | "wenyi.sidebarCollapsed" | "wenyi.lastProject",
      value: string,
    ) => void;
    subscribe: (key: "wenyi.locale", listener: () => void) => () => void;
  };
  request: <T>(path: string, init?: RequestInit) => Promise<T>;
  download: (path: string, fallback: string) => Promise<void>;
  apiBase: () => string;
  setAuthToken: (token: string | null) => void;
  connectProgress: (project: string) => WebSocket;
  authenticateProgress: (socket: WebSocket) => void;
  progressInterval: (connected: boolean, fallback: number) => number;
  progressKeys: (kind: string) => string[];
  bindSourceDrop: (zone: HTMLElement, callbacks: SourceDropCallbacks) => () => void;
  capabilities: {
    updates?: {
      Section: ComponentType<{ blocked: boolean; onInstalling: (value: boolean) => void }>;
    };
    saveExport?: (
      project: string, options: ExportOptions, exportId?: number,
    ) => Promise<{ path: string } | null>;
    credentials?: {
      Field: ComponentType<CredentialFieldProps>;
      refresh: () => Promise<unknown>;
    };
  };
}

let services: PlatformServices | undefined;
export function configurePlatform(value: PlatformServices) {
  if (services) throw new Error("Platform services are already configured.");
  services = value;
}
export function platform(): PlatformServices {
  if (!services) throw new Error("The application host must configure platform services.");
  return services;
}
