# Frontend localization

- `locales/en.ts` defines canonical, typed message keys; `locales/zh-CN.ts` implements Simplified Chinese.
- `catalog.ts` registers languages and creates translators with English fallback and named interpolation.
- `index.ts` exposes `useI18n()` for reactive components, `translate()` for non-React helpers, and the browser preference store.
- `DocumentLanguage.tsx` keeps the document language and title in sync.
- `labels.ts` maps known backend identifiers to interface messages without changing submitted identifiers or custom labels.
- `status.ts` maps task/chapter status codes to message keys and badge tones. Review-specific handling states and phase patterns live in `features/review/reviewData.ts`; their messages remain in the locale catalogs.

```tsx
const { t, locale, setLocale } = useI18n();
return <span>{t("glossary.selectedCount", { count: selected.size })}</span>;
```

Add new messages to both catalogs. Use semantic keys, whole sentences, and descriptive interpolation
names such as `{count}` or `{name}`. Numeric interpolation uses `Intl.NumberFormat`; dates should
use the selected `locale`. React renders messages as text, including substituted values; do not
insert translated HTML. For new plural-sensitive wording, add locale-aware plural handling instead
of concatenating a noun onto a count. Existing count messages use labels where possible.

Keep lookup tables as message keys, or evaluate translations during rendering. Do not translate
module-level constants once at import time: those values would remain stale after switching languages.
Call `useI18n()` in each component that renders translated text so it subscribes to preference changes.

Add a locale file with `satisfies Messages`, then register it in `catalog.ts`. No component changes
are required to expose it in the selector. Unsupported saved locale codes use English. Cross-tab
changes use the browser storage event; storage errors retain the selection in memory for the session.

UI language is separate from book language. Preserve user content, backend identifiers, server errors,
and live log text. See [the user guide](../../../../docs/web-i18n.md) and `tests/i18n.spec.ts`.
