import { Component, Suspense, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { useI18n } from "../i18n";
import { platform } from "../platform";
import { cn } from "../lib/utils";

/**
 * Shared failure presentation. Renders as a top-level `<main>` for the global
 * boundary and as a `<div>` inside the layout so a route failure never nests
 * interactive landmarks.
 */
export function ErrorFallback({ fullScreen = false }: { fullScreen?: boolean }) {
  const { t } = useI18n();
  const ExportDiagnostics = platform().capabilities.diagnostics?.ExportButton;
  const body = (
    <>
      {t("runtime.pageFailed")}{" "}
      <button className="underline" onClick={() => location.reload()}>
        {t("runtime.reload")}
      </button>
      {ExportDiagnostics && <div className="mt-4"><ExportDiagnostics /></div>}
    </>
  );
  // A failed route is a dead end, not a panel of content, so it gets the same
  // centred treatment as the desktop-only fallback instead of text pinned to
  // the top-left corner with a page of empty space beneath it.
  const shell =
    "flex flex-col items-center justify-center gap-3 p-8 text-center text-sm";
  return fullScreen ? (
    <main role="alert" className={cn("min-h-dvh", shell)}>
      {body}
    </main>
  ) : (
    <div role="alert" className={cn("min-h-64", shell)}>
      {body}
    </div>
  );
}

interface BoundaryProps {
  /** Changing the key clears a caught error (e.g. navigation to another route). */
  resetKey: string;
  children: ReactNode;
}

class RouteErrorBoundary extends Component<
  BoundaryProps,
  { failed: boolean; resetKey: string }
> {
  state = { failed: false, resetKey: this.props.resetKey };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch() {
    platform().capabilities.diagnostics?.reportRenderFailure();
  }
  static getDerivedStateFromProps(
    props: BoundaryProps,
    state: { failed: boolean; resetKey: string },
  ) {
    if (props.resetKey !== state.resetKey) {
      return { failed: false, resetKey: props.resetKey };
    }
    return null;
  }
  render() {
    return this.state.failed ? <ErrorFallback /> : this.props.children;
  }
}

/**
 * The single gate around route content: a per-route error boundary (reset on
 * navigation) wrapping a quiet Suspense gate. It lives inside the layout so the
 * shell stays mounted no matter how slowly a chunk or page settles.
 */
export function RouteGate({ children }: { children: ReactNode }) {
  const location = useLocation();
  return (
    <RouteErrorBoundary resetKey={location.pathname}>
      <Suspense
        fallback={
          <div
            aria-busy="true"
            className="route-placeholder"
            data-route-pending=""
          />
        }
      >
        {children}
      </Suspense>
    </RouteErrorBoundary>
  );
}
