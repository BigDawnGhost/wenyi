import { toast as sonner, type ExternalToast } from "sonner";

// Async results belong to the viewport, not the most recently clicked button.
// Brief confirmations are disposable; failures need time to read and dismiss.
export const toast = {
  success: (message: string, options?: ExternalToast) =>
    sonner.success(message, { duration: 1000, ...options }),
  error: (message: string, options?: ExternalToast) =>
    sonner.error(message, { duration: 10000, ...options }),
  info: (message: string, options?: ExternalToast) =>
    sonner.info(message, { duration: 5000, ...options }),
  warning: (message: string, options?: ExternalToast) =>
    sonner.warning(message, { duration: 10000, ...options }),
  message: (message: string, options?: ExternalToast) =>
    sonner.message(message, { duration: 5000, ...options }),
};
