export type Document = Record<string, unknown>;
export const object = (value: unknown) => (value || {}) as Document;
export type Renames = Record<string, string>;

// Connection IDs cannot contain "/", but remote model names (e.g. "org/model") can.
export function splitModelRef(ref: string) {
  const slash = ref.indexOf("/");
  return { provider: ref.slice(0, slash), model: ref.slice(slash + 1) };
}

export function nextId(prefix: string, ids: string[]) {
  let index = 1;
  while (ids.includes(`${prefix}${index}`)) index++;
  return `${prefix}${index}`;
}

export function renameSelections(llm: Document, renames: Renames): Document {
  const renamed = (ref: unknown) => typeof ref === "string" && Object.hasOwn(renames, ref) ? renames[ref] : ref;
  return {
    routes: Object.fromEntries(Object.entries(object(llm.routes)).map(([id, raw]) => {
      const route = object(raw);
      return [id, { ...route, model: renamed(route.model),
        ...(Array.isArray(route.fallbacks) ? { fallbacks: route.fallbacks.map(renamed) } : {}) }];
    })),
  };
}

export function isModelReferenced(llm: Document, id: string): boolean {
  return Object.values(object(llm.routes)).some(raw => {
    const route = object(raw);
    return route.model === id ||
      (Array.isArray(route.fallbacks) && route.fallbacks.includes(id));
  });
}
