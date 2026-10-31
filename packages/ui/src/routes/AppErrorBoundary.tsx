import { Component, type ReactNode } from "react";
import { ErrorFallback } from "./RouteGate";
import { platform } from "@/platform";

/**
 * Root-level safety net for failures outside the route content (providers,
 * layout shell). Route-level failures are handled inside the layout by
 * RouteGate, so this only fires when the shell itself cannot render.
 */
export class AppErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch() {
    platform().capabilities.diagnostics?.reportRenderFailure();
  }
  render() {
    return this.state.failed ? <ErrorFallback fullScreen /> : this.props.children;
  }
}
