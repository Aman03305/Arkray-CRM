"use client";

import {
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useId,
  useState,
} from "react";

interface FieldChrome {
  label: string;
  /** Messages from the API or client-side validation, shown under the control. */
  errors?: readonly string[];
  hint?: ReactNode;
  optional?: boolean;
}

const CONTROL =
  "block w-full rounded-md border bg-white px-3 text-sm text-slate-900 placeholder:text-slate-400 disabled:bg-slate-50 disabled:text-slate-500";

function controlBorder(invalid: boolean): string {
  return invalid
    ? "border-red-400 focus-visible:outline-red-600"
    : "border-slate-300 focus-visible:outline-brand-600";
}

function useDescribedBy(hint: ReactNode, errors: readonly string[] | undefined) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = errors?.length ? `${id}-error` : undefined;
  return { id, hintId, errorId, describedBy: [errorId, hintId].filter(Boolean).join(" ") || undefined };
}

function Label({ htmlFor, label, optional }: { htmlFor: string; label: string; optional?: boolean }) {
  return (
    <label htmlFor={htmlFor} className="mb-1.5 block text-sm font-medium text-slate-700">
      {label}
      {optional ? <span className="font-normal text-slate-500"> (optional)</span> : null}
    </label>
  );
}

function Messages({ hint, hintId, errors, errorId }: {
  hint?: ReactNode;
  hintId?: string;
  errors?: readonly string[];
  errorId?: string;
}) {
  return (
    <>
      {errors?.length ? (
        <p id={errorId} className="mt-1.5 text-xs text-red-600">
          {errors.join(" ")}
        </p>
      ) : null}
      {hint ? (
        <p id={hintId} className="mt-1.5 text-xs text-slate-500">
          {hint}
        </p>
      ) : null}
    </>
  );
}

type TextFieldProps = FieldChrome & Omit<InputHTMLAttributes<HTMLInputElement>, "id">;

export function TextField({ label, errors, hint, optional, className = "", ...input }: TextFieldProps) {
  const { id, hintId, errorId, describedBy } = useDescribedBy(hint, errors);
  const invalid = Boolean(errors?.length);
  return (
    <div className={className}>
      <Label htmlFor={id} label={label} optional={optional} />
      <input
        id={id}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        className={`${CONTROL} h-9 ${controlBorder(invalid)}`}
        {...input}
      />
      <Messages hint={hint} hintId={hintId} errors={errors} errorId={errorId} />
    </div>
  );
}

type PasswordFieldProps = FieldChrome & Omit<InputHTMLAttributes<HTMLInputElement>, "id" | "type">;

export function PasswordField({ label, errors, hint, optional, className = "", ...input }: PasswordFieldProps) {
  const { id, hintId, errorId, describedBy } = useDescribedBy(hint, errors);
  const [visible, setVisible] = useState(false);
  const invalid = Boolean(errors?.length);
  return (
    <div className={className}>
      <Label htmlFor={id} label={label} optional={optional} />
      <div className="relative">
        <input
          id={id}
          type={visible ? "text" : "password"}
          aria-invalid={invalid || undefined}
          aria-describedby={describedBy}
          spellCheck={false}
          autoCapitalize="none"
          className={`${CONTROL} h-9 pr-16 ${controlBorder(invalid)}`}
          {...input}
        />
        <button
          type="button"
          onClick={() => setVisible((v) => !v)}
          aria-pressed={visible}
          aria-controls={id}
          aria-label={visible ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`}
          className="absolute inset-y-0 right-0 flex items-center rounded-r-md px-3 text-xs font-medium text-slate-500 hover:text-slate-800"
        >
          {visible ? "Hide" : "Show"}
        </button>
      </div>
      <Messages hint={hint} hintId={hintId} errors={errors} errorId={errorId} />
    </div>
  );
}

type SelectFieldProps = FieldChrome &
  Omit<SelectHTMLAttributes<HTMLSelectElement>, "id"> & {
    options: readonly { value: string; label: string }[];
  };

export function SelectField({ label, errors, hint, optional, options, className = "", ...select }: SelectFieldProps) {
  const { id, hintId, errorId, describedBy } = useDescribedBy(hint, errors);
  const invalid = Boolean(errors?.length);
  return (
    <div className={className}>
      <Label htmlFor={id} label={label} optional={optional} />
      <select
        id={id}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        className={`${CONTROL} h-9 ${controlBorder(invalid)}`}
        {...select}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <Messages hint={hint} hintId={hintId} errors={errors} errorId={errorId} />
    </div>
  );
}

type TextAreaFieldProps = FieldChrome & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "id">;

export function TextAreaField({ label, errors, hint, optional, className = "", ...textarea }: TextAreaFieldProps) {
  const { id, hintId, errorId, describedBy } = useDescribedBy(hint, errors);
  const invalid = Boolean(errors?.length);
  return (
    <div className={className}>
      <Label htmlFor={id} label={label} optional={optional} />
      <textarea
        id={id}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        className={`${CONTROL} py-2 ${controlBorder(invalid)}`}
        {...textarea}
      />
      <Messages hint={hint} hintId={hintId} errors={errors} errorId={errorId} />
    </div>
  );
}
