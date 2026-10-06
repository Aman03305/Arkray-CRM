"use client";

import { useCallback, useRef } from "react";

/**
 * Runs a submission unless the previous one this guard started for the same key is still
 * running.
 *
 * A form can't guard itself with its mutation's `isPending`, nor with Button's `loading`:
 * TanStack Query publishes the pending state on a later tick (setTimeout 0) and React shows
 * it after that, so two activations in a row (a machine-speed double click, Enter then a
 * click) both see "not pending" and both send. The final audit saw two POSTs from one double
 * click (UI-5); for a password change, a reset link or an edit carrying a version, the second
 * request fails and its error replaces the first one's success. A ref is set synchronously.
 *
 * `start` returns the submission's promise (`mutation.mutateAsync(...)`); the guard opens
 * again when it settles, whether it succeeded or failed. `key` keeps one row's action from
 * blocking another's (Complete on two tasks). Returns false when refused.
 */
export function useSingleFlight(): (start: () => Promise<unknown>, key?: string) => boolean {
  const running = useRef(new Set<string>());
  return useCallback((start: () => Promise<unknown>, key = "") => {
    if (running.current.has(key)) return false;
    running.current.add(key);
    const release = () => {
      running.current.delete(key);
    };
    try {
      start().then(release, release);
    } catch (error) {
      release();
      throw error;
    }
    return true;
  }, []);
}
