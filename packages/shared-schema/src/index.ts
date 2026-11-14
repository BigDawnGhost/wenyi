/** Generated HTTP contracts. Regenerate from the running API with `pnpm gen:schema`. */
export type { components, operations, paths } from "./api";
/** Desktop-added routes. A separate document, so the shared contract stays platform free. */
export type {
  components as desktopComponents,
  operations as desktopOperations,
  paths as desktopPaths,
} from "./desktop";

/** Pipeline mode stored inside the editable configuration document. */
export type TranslationMode = "standard" | "three_draft_synthesis";
