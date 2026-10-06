# Overlay and notification behavior

Shared dialogs use a body-portaled Radix Dialog, with an accessible title,
optional description, initial/return focus and a disabled-close state while
required by a workflow. The centered overlay retains a 2px backdrop blur.
Portaled Radix Select options remain usable above the dialog; native
`showModal()` is not used because its browser top layer would cover them.

Mount `NotificationToaster` once in the unified application shell, outside
scrolling route content. Its body portal uses Sonner's viewport-fixed top-right
stack, above dialogs and dropdowns, with interactive close controls.

Async feedback deliberately does **not** follow a button. A global last-click
heuristic cannot identify the source of a delayed result and makes unrelated
clicks or scrolling move feedback. The consistent viewport position trades
proximity to the initiating action for visibility and predictable placement.
Success confirmations last one second; errors and warnings last ten seconds;
informational messages last five seconds. Callers can override duration for
special cases. Add/edit glossary failures also remain inline in the open form
until retry or a new editing session, so the user can review and correct input
even after the notification expires.

Focused mock-only Web/Desktop regressions cover glossary option selection,
inline save failures, readable modal notifications, and slow feedback across
content scrolling and unrelated clicks. No model or external service is used.
