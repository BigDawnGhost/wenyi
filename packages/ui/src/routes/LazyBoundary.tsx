import { Suspense, type ReactNode } from "react";

/**
 * Standard wrapper for a `lazy()` component: keeps the suspense boundary at the
 * smallest possible scope so a slow chunk only blanks its own slot, never the
 * surrounding page. Pair every `lazy(` call with this boundary (or register the
 * loader for warm-up) — enforced by packages/ui/tests/routing-rules.test.mjs.
 */
export function LazyBoundary({
  fallback = null,
  children,
}: {
  fallback?: ReactNode;
  children: ReactNode;
}) {
  return <Suspense fallback={fallback}>{children}</Suspense>;
}
