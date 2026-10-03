"use client";

import { type RefObject, useEffect } from "react";

/**
 * After a failed submit, move focus to the first invalid field, so keyboard and
 * screen-reader users land on the problem (its message is its accessible description).
 * `trigger` changes on every validation outcome (client errors object, server error).
 */
export function useFocusFirstInvalid(formRef: RefObject<HTMLFormElement | null>, trigger: unknown): void {
  useEffect(() => {
    if (!trigger) return;
    formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [formRef, trigger]);
}
