import type { ComponentType } from "react";
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
  storedConnection?: string;
  /** Undefined leaves the stored key unchanged; an empty edit clears it on save. */
  edit?: string;
  onEdit: (edit: string | undefined) => void;
}

export interface ActivityServices {
  isForeground: () => boolean;
  subscribe: (listener: () => void) => () => void;
}

type PreferenceKey = "wenyi.locale" | "wenyi.sidebarCollapsed" | "wenyi.lastProject" | "wenyi.theme";
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
}

/** What a host can say about adopting a project folder that already exists.
 *
 * A native host can open a folder picker; the Web host cannot, so the entry
 * point is absent there rather than shown and disabled. `choose` answers the
 * selected absolute path, or null when the picker was dismissed.
 */
export interface LibraryServices {
  choose: () => Promise<string | null>;
  /** Adopt the chosen folder; the message of a refusal is written for a person. */
  importProject: (path: string) => Promise<ProjectDetail>;
  /**
   * Say whether a folder may hold a new project, before one is created.
   *
   * A picker can name any folder the operating system allows, including ones the
   * layout must refuse. Asking here is what lets the dialog explain a refusal
   * while the choice is still on screen, rather than after an upload.
   */
  admitLocation?: (path: string) => Promise<string>;
}

export interface OptionalCapabilities {
  library?: LibraryServices;
  /** Host-owned rows inside General settings; absent hosts render no controls. */
  generalSettings?: ComponentType;
  diagnostics?: {
    label: (locale: string) => string;
    Section: ComponentType;
    ExportButton: ComponentType;
    /** No exception object or user text crosses this optional observation hook. */
    reportRenderFailure: () => void;
    /**
     * Record a defect in data a host delivered, never the payload itself.
     *
     * The channel is a fixed name so a native diagnostic log stays free of
     * translated text, frame contents and user input. Hosts without a
     * diagnostic sink omit it; the shared UI then reports nothing.
     */
    reportDiagnostic?: (channel: string, error: unknown) => void;
  };
  externalCredentials?: {
    MinerU: ComponentType;
  };
  updates?: {
    label: (locale: string) => string;
    Section: ComponentType<{ blocked: boolean; onInstalling: (value: boolean) => void }>;
  };
  plugins?: {
    label: (locale: string) => string;
    Section: ComponentType<{ disabled: boolean }>;
  };
  saveExport?: (
    project: string, options: ExportOptions, exportId?: number,
  ) => Promise<{ path: string } | null>;
  credentials?: {
    Field: ComponentType<CredentialFieldProps>;
    refresh: () => Promise<unknown>;
    /** Write only this dialog's credential, after its connection exists. */
    save: (connection: string, key: string) => Promise<void>;
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
