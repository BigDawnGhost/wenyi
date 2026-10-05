import { Component, type ReactNode } from "react";
import { ErrorFallback } from "./RouteGate";

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
  render() {
    return this.state.failed ? <ErrorFallback fullScreen /> : this.props.children;
  }
}
