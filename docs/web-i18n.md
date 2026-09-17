# Web interface languages

[简体中文](zh/web-i18n.md) · [Web deployment](web.md)

The Web interface defaults to English, regardless of the browser's preferred language.
Open global **Settings → Interface language** to choose **English** or **简体中文**.
Interface language is a browser preference available only in global settings.
The global settings page is available before creating any projects, from mobile navigation,
and while a project task is running.

The selection applies immediately, survives reloads, and synchronizes between tabs on the
same site. It is stored in the browser under `wenyi.locale`, rather than in project
configuration. An absent or unsupported saved language falls back to English.

Interface language controls navigation, forms, notifications, built-in workflow labels,
page titles, and localized number/date formatting. It does not change translation source
or target languages, model configuration, API identifiers, original text, translations,
glossary entries, or model-generated analysis. Server errors and live log messages are
shown as received. Workflow details preserve unknown/custom labels. The review activity
summary localizes known phases and uses neutral status wording when the phase is unknown.

## Adding a language

Frontend localization is managed in [`apps/web/src/i18n/`](../apps/web/src/i18n/README.md):

1. Add a locale file under `locales/`, using `locales/en.ts` as the canonical list of message keys.
2. Type the dictionary with `satisfies Messages` and preserve each message's interpolation names.
3. Register its BCP 47 code, native language name, and dictionary in `catalog.ts`. The selector
   and `Locale` type derive from this registry.
4. Run the frontend type check, build, and Playwright tests. The catalog test checks that
   all languages have matching keys and interpolation parameters.

Use complete messages for sentences containing counts or names, rather than joining
translated fragments. Keep source/target content separate from interface messages.

## Lists and status labels

Chapter proofreading uses searchable rows with a translation-status filter and saved
paragraph counts. Whole-book review shows searchable rows filtered by handling status:
**Needs attention**, **Written back**, **Fix failed**, or **Unchanged**. Expand a row for
evidence, suggested text and publication details, or jump to its paragraph in proofreading.
Translation completion does not imply manual proofreading.

Task/chapter status labels and badge colors are shared in `src/i18n/status.ts` and `StatusBadge`.
Both `done` and `completed` display as “Completed”; chapter `pending` displays as
“Awaiting translation”. Unknown codes display “Unknown status”; original codes remain
available in raw run details. This changes presentation only, not API or stored state.

Review handling labels and phase patterns are mapped in `src/features/review/reviewData.ts`;
the translated messages still belong to the locale catalogs. Paragraph-history labels also
follow the interface language, while all historical text remains unchanged. History records
text changes, so an absent polishing entry does not establish that polishing was skipped.
