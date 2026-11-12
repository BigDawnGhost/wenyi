/** Keep the React runtime cacheable without merging lazy pages or other vendors. */
export function frontendManualChunks(id: string): string | undefined {
  const normalized = id.replace(/\\/g, "/");
  if (/\/node_modules\/(?:react|react-dom|scheduler)\//.test(normalized)) {
    return "react-runtime";
  }
}
