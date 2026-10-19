import { useEffect, useState } from "react";

export async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return (await res.json()) as T;
}

export type Loadable<T> = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; data: T };

/** Fetch one endpoint; refetches when `path` changes and ignores responses for a stale path. */
export function useApi<T>(path: string | null): Loadable<T> {
  const [state, setState] = useState<Loadable<T>>({ status: "loading" });
  useEffect(() => {
    if (!path) return;
    const ctrl = new AbortController();
    setState({ status: "loading" });
    getJSON<T>(path, ctrl.signal).then(
      (data) => setState({ status: "ready", data }),
      (err: unknown) => {
        if (!ctrl.signal.aborted) setState({ status: "error", message: String((err as Error)?.message ?? err) });
      },
    );
    return () => ctrl.abort();
  }, [path]);
  return state;
}
