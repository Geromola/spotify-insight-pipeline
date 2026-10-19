import type { ReactNode } from "react";

import type { Loadable } from "../api.ts";

/** Renders children once the data is ready, with quiet loading and error states otherwise. */
export function Status<T>({ state, what, children }: { state: Loadable<T>; what: string; children: (data: T) => ReactNode }) {
  if (state.status === "loading") {
    return (
      <p className="muted" role="status">
        Loading {what}…
      </p>
    );
  }
  if (state.status === "error") {
    return (
      <p className="error" role="alert">
        Could not load {what}. Please refresh the page.
      </p>
    );
  }
  return <>{children(state.data)}</>;
}
