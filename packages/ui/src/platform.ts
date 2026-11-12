/** Stable host entry point; contracts do not depend on the API client. */
export type * from "./platform/contracts";
export { configurePlatform, platform, releaseSource } from "./platform/runtime";
