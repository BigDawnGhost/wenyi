import { createElement, type ReactNode } from "react";
import { toast as sonner, type ExternalToast } from "sonner";

function notificationText(text: string) {
  return createElement("span", { className: "notification-text" }, text);
}

function notificationDescription(description: ExternalToast["description"]) {
  const wrapText = (value: ReactNode) =>
    typeof value === "string" && value ? notificationText(value) : value;
  return typeof description === "function" ? () => wrapText(description()) : wrapText(description);
}

function withDuration(notify: typeof sonner.success, duration: number) {
  return (message: string, { description, ...options }: ExternalToast = {}) =>
    // A render function keeps Sonner's standard layout; passing JSX directly
    // as the title would take its custom-content branch.
    notify(() => notificationText(message), {
      duration,
      ...options,
      description: notificationDescription(description),
    });
}

// Async results belong to the viewport, not the most recently clicked button.
// Brief confirmations are disposable; failures need time to read and dismiss.
export const toast = {
  success: withDuration(sonner.success, 1000),
  error: withDuration(sonner.error, 10000),
  info: withDuration(sonner.info, 5000),
  warning: withDuration(sonner.warning, 10000),
  message: withDuration(sonner.message, 5000),
};
