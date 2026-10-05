"use client";

import { useId } from "react";

import { SelectField, TextAreaField, TextField } from "@/components/ui/Field";
import type { CustomField } from "@/lib/api/types";
import { formatDateOnly } from "@/lib/format";
import { formatInr } from "@/lib/money";

import type { CustomValue } from "./draft";

/** One input per custom field of the pipeline, in order. Values stay text (exact decimals). */
export function CustomFieldInputs({
  fields,
  values,
  onChange,
  errors,
}: {
  fields: readonly CustomField[];
  values: Record<string, CustomValue>;
  onChange: (id: string, value: CustomValue) => void;
  errors: Record<string, readonly string[] | undefined>;
}) {
  return (
    <>
      {fields.map((field) => (
        <CustomFieldInput
          key={field.id}
          field={field}
          value={values[field.id] ?? null}
          onChange={(value) => onChange(field.id, value)}
          errors={errors[`custom_fields.${field.id}`]}
        />
      ))}
    </>
  );
}

function CustomFieldInput({
  field,
  value,
  onChange,
  errors,
}: {
  field: CustomField;
  value: CustomValue;
  onChange: (value: CustomValue) => void;
  errors: readonly string[] | undefined;
}) {
  const groupId = useId();
  const optional = !field.required;
  const name = `custom_fields.${field.id}`;
  const text = typeof value === "string" ? value : "";
  switch (field.type) {
    case "long_text":
      return (
        <TextAreaField
          className="sm:col-span-2"
          label={field.name}
          name={name}
          optional={optional}
          rows={3}
          maxLength={5000}
          value={text}
          onChange={(e) => onChange(e.target.value)}
          errors={errors}
        />
      );
    case "number":
      return <TextField label={field.name} name={name} optional={optional} inputMode="decimal" value={text} onChange={(e) => onChange(e.target.value)} errors={errors} autoComplete="off" />;
    case "currency":
      return <TextField label={`${field.name} (₹)`} name={name} optional={optional} inputMode="decimal" value={text} onChange={(e) => onChange(e.target.value)} errors={errors} autoComplete="off" />;
    case "date":
      return <TextField label={field.name} name={name} optional={optional} type="date" min="1900-01-01" max="2199-12-31" value={text} onChange={(e) => onChange(e.target.value)} errors={errors} />;
    case "boolean":
      return (
        <SelectField
          label={field.name}
          name={name}
          optional={optional}
          value={value === true ? "yes" : value === false ? "no" : ""}
          onChange={(e) => onChange(e.target.value === "yes" ? true : e.target.value === "no" ? false : null)}
          options={[
            { value: "", label: "—" },
            { value: "yes", label: "Yes" },
            { value: "no", label: "No" },
          ]}
          errors={errors}
        />
      );
    case "single_select":
      return (
        <SelectField
          label={field.name}
          name={name}
          optional={optional}
          value={text}
          onChange={(e) => onChange(e.target.value)}
          options={[{ value: "", label: "—" }, ...field.options.map((o) => ({ value: o.id, label: o.label }))]}
          errors={errors}
        />
      );
    case "multi_select": {
      const chosen = Array.isArray(value) ? value : [];
      const errorId = `${groupId}-errors`;
      return (
        <fieldset className="sm:col-span-2" aria-describedby={errors?.length ? errorId : undefined} data-invalid={errors?.length ? "" : undefined}>
          <legend className="mb-1 text-sm font-medium text-slate-700">
            {field.name}
            {optional ? <span className="ml-1 font-normal text-slate-500">(optional)</span> : null}
          </legend>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {field.options.map((option) => (
              <label key={option.id} className="flex items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  name={name}
                  className="size-4 accent-brand-600"
                  checked={chosen.includes(option.id)}
                  onChange={(e) => onChange(e.target.checked ? [...chosen, option.id] : chosen.filter((id) => id !== option.id))}
                />
                {option.label}
              </label>
            ))}
          </div>
          {errors?.length ? (
            <p id={errorId} className="mt-1 text-xs text-red-600">
              {errors[0]}
            </p>
          ) : null}
        </fieldset>
      );
    }
    default:
      return <TextField label={field.name} name={name} optional={optional} maxLength={500} value={text} onChange={(e) => onChange(e.target.value)} errors={errors} autoComplete="off" />;
  }
}

/** A stored custom value as text for reading (the deal page). */
export function formatCustomValue(field: CustomField, value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  switch (field.type) {
    case "boolean":
      return value === true ? "Yes" : value === false ? "No" : "—";
    case "currency":
      return typeof value === "string" ? formatInr(value) : "—";
    case "date":
      return typeof value === "string" ? formatDateOnly(value) : "—";
    case "single_select":
      return field.options.find((o) => o.id === value)?.label ?? "(removed choice)";
    case "multi_select":
      return Array.isArray(value)
        ? value.map((id) => field.options.find((o) => o.id === id)?.label ?? "(removed choice)").join(", ") || "—"
        : "—";
    default:
      return typeof value === "string" ? value : "—";
  }
}
