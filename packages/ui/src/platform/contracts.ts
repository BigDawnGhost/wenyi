import type { ComponentType, ReactNode } from "react";
import type { ProjectDetail, ProjectInput, ExportOptions } from "../api/types";

export type { ProjectInput, ExportOptions } from "../api/types";

/** An opaque selected-source capability, not a filesystem path. */
export interface SourceCapability {
  name: string;
  size: number;
  upload: (project: ProjectInput) => Promise<ProjectDetail>;
  release: () => void;
}
export type ProjectSource = File | SourceCapability;

export interface SourceDropCallbacks {
  disabled: boolean;
  select: (source: ProjectSource) => boolean;
  dragging: (value: boolean) => void;
  error: (message: string) => void;
}

export interface CredentialFieldProps {
  connection: string;
  children?: ReactNode;
}

export interface ActivityServices {
  isForeground: () => boolean;
  subscribe: (listener: () => void) => () => void;
}

type PreferenceKey = "wenyi.locale" | "wenyi.sidebarCollapsed" | "wenyi.lastProject";
export interface PreferenceServices {
  get: (key: PreferenceKey) => string | null;
  set: (key: PreferenceKey, value: string) => void;
  subscribe: (key: "wenyi.locale", listener: () => void) => () => void;
}

export interface TransportServices {
  request: <T>(path: string, init?: RequestInit) => Promise<T>;
  /** Native hosts return the saved destination, or null when the picker is canceled. */
  download: (path: string, fallback: string) => Promise<{ path: string } | null | void>;
  apiBase: () => string;
  setAuthToken: (token: string | null) => void;
}

export interface ProgressServices {
  connectProgress: (project: string) => WebSocket;
  authenticateProgress: (socket: WebSocket) => void;
  progressInterval: (connected: boolean, fallback: number) => number;
  progressKeys: (kind: string) => string[];
}

export interface OptionalCapabilities {
  diagnostics?: {
    Section: ComponentType;
    ExportButton: ComponentType;
    /** No exception object or user text crosses this optional observation hook. */
    reportRenderFailure: () => void;
  };
  externalCredentials?: {
    MinerU: ComponentType;
  };
  updates?: {
    Section: ComponentType<{ blocked: boolean; onInstalling: (value: boolean) => void }>;
  };
  plugins?: {
    Section: ComponentType<{ disabled: boolean }>;
  };
  saveExport?: (
    project: string, options: ExportOptions, exportId?: number,
  ) => Promise<{ path: string } | null>;
  credentials?: {
    Field: ComponentType<CredentialFieldProps>;
    refresh: () => Promise<unknown>;
    /** Capture entered keys now; invoke the returned write after configuration is saved. */
    prepareCommit: () => (connections: string[]) => Promise<void>;
    /** Discards entered keys for connections that are no longer in the draft. */
    retain: (connections: string[]) => void;
    /** Keeps an entered key attached to a connection whose ID is renamed before saving. */
    rename: (from: string, to: string) => void;
    /** True while entered keys have not been written yet. */
    usePending: () => boolean;
    /** The key currently in a connection's field, used to list models before saving. */
    forListing: (connection: string) => string | undefined;
  };
}

/** Selected once by the host before mounting React; no implicit browser fallback. */
export interface PlatformServices extends TransportServices, ProgressServices {
  /** Hosts that suspend background updates reconcile durable state on return. */
  activity: ActivityServices;
  preferences: PreferenceServices;
  bindSourceDrop: (zone: HTMLElement, callbacks: SourceDropCallbacks) => () => void;
  capabilities: OptionalCapabilities;
}
