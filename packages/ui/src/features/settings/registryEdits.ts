export type Document = Record<string, unknown>;
export const object = (value: unknown) => (value || {}) as Document;
export type RegistryGroup = "providers" | "models";

export function renameRegistryId(
  llm: Document,
  group: RegistryGroup,
  oldId: string,
  newId: string,
): Document {
  const renamed: Document = {
    ...llm,
    preset: null,
    [group]: Object.fromEntries(
      Object.entries(object(llm[group])).map(([id, value]) => [
        id === oldId ? newId : id,
        value,
      ]),
    ),
  };
  if (group === "providers") {
    renamed.models = Object.fromEntries(
      Object.entries(object(llm.models)).map(([id, raw]) => {
        const model = object(raw);
        return [
          id,
          model.provider === oldId ? { ...model, provider: newId } : model,
        ];
      }),
    );
  } else {
    renamed.tiers = Object.fromEntries(
      Object.entries(object(llm.tiers)).map(([tier, id]) => [
        tier,
        id === oldId ? newId : id,
      ]),
    );
    renamed.routes = Object.fromEntries(
      Object.entries(object(llm.routes)).map(([id, raw]) => {
        const route = object(raw);
        return [
          id,
          {
            ...route,
            ...(route.model === oldId ? { model: newId } : {}),
            ...(Array.isArray(route.fallbacks)
              ? {
                  fallbacks: route.fallbacks.map((id) =>
                    id === oldId ? newId : id,
                  ),
                }
              : {}),
          },
        ];
      }),
    );
  }
  return renamed;
}

export function registryReferences(
  llm: Document,
  group: RegistryGroup,
  id: string,
): string[] {
  if (group === "providers")
    return Object.entries(object(llm.models))
      .filter(([, model]) => object(model).provider === id)
      .map(([key]) => key);
  return [
    ...Object.entries(object(llm.tiers))
      .filter(([, model]) => model === id)
      .map(([key]) => key),
    ...Object.entries(object(llm.routes))
      .filter(([, raw]) => {
        const route = object(raw);
        return (
          route.model === id ||
          (Array.isArray(route.fallbacks) && route.fallbacks.includes(id))
        );
      })
      .map(([key]) => key),
  ];
}
