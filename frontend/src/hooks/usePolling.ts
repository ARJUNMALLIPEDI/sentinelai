import { useEffect, useRef, useState } from "react";

export interface PollState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  lastUpdated: Date | null;
}

/**
 * Poll an async fetcher on an interval.
 *
 * - Never overlaps requests: the next poll is scheduled only after the
 *   previous one settles, so a slow backend can't cause a request pile-up.
 * - Aborts the in-flight request on unmount.
 * - Keeps the last good data when a poll fails, so charts don't blank out on
 *   a transient error; `error` reports the failure alongside it.
 *
 * Polling is the simplest correct choice for Milestone 1. Server-sent events
 * are a candidate for live incident updates later (see ARCHITECTURE.md).
 */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
): PollState<T> {
  const [state, setState] = useState<PollState<T>>({
    data: null,
    error: null,
    loading: true,
    lastUpdated: null,
  });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      try {
        const data = await fetcherRef.current(controller.signal);
        setState({ data, error: null, loading: false, lastUpdated: new Date() });
      } catch (err) {
        if (controller.signal.aborted) return;
        setState((s) => ({ ...s, error: err as Error, loading: false }));
      }
      if (!controller.signal.aborted) timer = setTimeout(tick, intervalMs);
    };

    void tick();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [intervalMs]);

  return state;
}
