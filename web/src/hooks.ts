import { useCallback, useEffect, useMemo, useState } from "react";

type AsyncResult<T> =
  | { status: "loading"; data?: T; error?: undefined }
  | { status: "success"; data: T; error?: undefined }
  | { status: "error"; data?: undefined; error: Error };

export type AsyncState<T> = AsyncResult<T> & { retry: () => void };

type AsyncSnapshot<T> = {
  attempt: number;
  dependencies: readonly unknown[];
  result: AsyncResult<T>;
};

const LOADING_RESULT = { status: "loading" } as const;

function sameDependencies(previous: readonly unknown[], current: readonly unknown[]) {
  return (
    previous.length === current.length &&
    previous.every((value, index) => Object.is(value, current[index]))
  );
}

export function useAsync<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): AsyncState<T> {
  const [attempt, setAttempt] = useState(0);
  const [snapshot, setSnapshot] = useState<AsyncSnapshot<T>>({
    attempt,
    dependencies: [...deps],
    result: LOADING_RESULT,
  });
  const retry = useCallback(() => setAttempt((current) => current + 1), []);
  const result =
    snapshot.attempt === attempt && sameDependencies(snapshot.dependencies, deps)
      ? snapshot.result
      : LOADING_RESULT;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const dependencies = [...deps];
    loader(controller.signal).then(
      (data) => {
        if (active) {
          setSnapshot({
            attempt,
            dependencies,
            result: { status: "success", data },
          });
        }
      },
      (error: unknown) => {
        if (active && !(error instanceof DOMException && error.name === "AbortError")) {
          setSnapshot({
            attempt,
            dependencies,
            result: {
              status: "error",
              error: error instanceof Error ? error : new Error(String(error)),
            },
          });
        }
      },
    );
    return () => {
      active = false;
      controller.abort();
    };
    // Callers provide a stable loader dependency list intentionally.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, attempt]);

  return useMemo(() => ({ ...result, retry }), [result, retry]);
}

export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedValue(value), delayMs);
    return () => window.clearTimeout(timeout);
  }, [delayMs, value]);

  return debouncedValue;
}
