"use client";

import { Check, GripVertical, X } from "lucide-react";
import { type DragEvent, type KeyboardEvent, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";

/** What a dragged instrument carries: only drops of our own options are accepted. */
export const INSTRUMENT_DRAG_TYPE = "application/x-arkray-instrument";

interface InstrumentPickerProps {
  /** The instruments offered: the server's list (pipeline.instruments), in its order. */
  instruments: readonly string[];
  value: string;
  onChange: (value: string) => void;
  errors?: readonly string[];
  status?: "loading" | "error" | "ready";
  onRetry?: () => void;
  /** A retry is under way (after an error). */
  retrying?: boolean;
}

/**
 * The opportunity's instrument: a list of the instruments and a "Selected" box. Choose one by
 * tapping or clicking it, with the keyboard (it is a radio group: Tab in, arrow keys to move
 * and choose, Space or Enter), or by dragging it into the box. Dragging is only a shortcut on
 * desktop; nothing requires it. The box's × clears the choice (the instrument is optional).
 * An opportunity saved before the list existed shows its own text until another is chosen.
 */
export function InstrumentPicker({ instruments, value, onChange, errors, status = "ready", onRetry, retrying = false }: InstrumentPickerProps) {
  const id = useId();
  const labelId = `${id}-label`;
  const errorId = `${id}-error`;
  const radios = useRef<(HTMLDivElement | null)[]>([]);
  const [dragging, setDragging] = useState(false);
  const [over, setOver] = useState(false);
  const invalid = Boolean(errors?.length);
  const listed = instruments.includes(value);
  const tabbable = listed ? instruments.indexOf(value) : 0;
  // Only a loaded list can say a value isn't on it (not while it loads or after it failed).
  const unlisted = status === "ready" && Boolean(value) && !listed;
  // After the choice is removed nothing is chosen, so the first instrument is the tab stop.
  const focusFirst = () => requestAnimationFrame(() => radios.current[0]?.focus());

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>, index: number) => {
    const last = instruments.length - 1;
    let next: number;
    switch (event.key) {
      case "ArrowDown":
      case "ArrowRight":
        next = index === last ? 0 : index + 1;
        break;
      case "ArrowUp":
      case "ArrowLeft":
        next = index === 0 ? last : index - 1;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = last;
        break;
      case " ":
      case "Enter":
        event.preventDefault();
        onChange(instruments[index]!);
        return;
      default:
        return;
    }
    event.preventDefault();
    radios.current[next]?.focus();
    onChange(instruments[next]!);
  };

  const accepts = (event: DragEvent) => event.dataTransfer.types.includes(INSTRUMENT_DRAG_TYPE);
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setOver(false);
    setDragging(false);
    const name = event.dataTransfer.getData(INSTRUMENT_DRAG_TYPE);
    if (instruments.includes(name)) onChange(name);
  };

  return (
    <div className="sm:col-span-2">
      <span id={labelId} className="mb-1.5 block text-sm font-medium text-slate-700">
        Instrument name <span className="font-normal text-slate-500">(optional)</span>
      </span>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="min-w-0">
          <p className="mb-1 text-xs font-medium text-slate-500">Available</p>
          {status === "loading" ? (
            <div aria-busy="true" className="space-y-1.5">
              {Array.from({ length: 4 }, (_, i) => (
                <Skeleton key={i} className="h-9 w-full" />
              ))}
              <span className="sr-only">Loading instruments</span>
            </div>
          ) : status === "error" ? (
            <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-red-700">
              Instruments couldn&apos;t be loaded.
              {onRetry ? (
                <Button variant="secondary" size="sm" onClick={onRetry} loading={retrying}>
                  Try again
                </Button>
              ) : null}
            </div>
          ) : (
            <div
              role="radiogroup"
              aria-labelledby={labelId}
              aria-invalid={invalid || undefined}
              aria-describedby={invalid ? errorId : undefined}
              data-field="instrument_name"
              tabIndex={-1}
              // Focus sent to the group (the first problem after a refused save) goes on to its
              // tab stop, so arrow keys work at once (radio group pattern; frontend review).
              onFocus={(event) => {
                if (event.target === event.currentTarget) radios.current[tabbable]?.focus();
              }}
              className="space-y-1.5 focus:outline-none"
            >
              {instruments.map((name, index) => {
                const checked = name === value;
                return (
                  <div
                    key={name}
                    ref={(element) => {
                      radios.current[index] = element;
                    }}
                    role="radio"
                    aria-checked={checked}
                    tabIndex={index === tabbable ? 0 : -1}
                    draggable
                    onClick={() => onChange(name)}
                    onKeyDown={(event) => onKeyDown(event, index)}
                    onDragStart={(event) => {
                      event.dataTransfer.setData(INSTRUMENT_DRAG_TYPE, name);
                      event.dataTransfer.setData("text/plain", name);
                      event.dataTransfer.effectAllowed = "copy";
                      setDragging(true);
                    }}
                    onDragEnd={() => {
                      setDragging(false);
                      setOver(false);
                    }}
                    className={`flex min-h-10 cursor-pointer select-none items-center gap-2 rounded-md border px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand-600 sm:cursor-grab ${
                      checked
                        ? "border-brand-600 bg-brand-50 font-medium text-brand-800"
                        : "border-slate-300 bg-white text-slate-800 hover:border-slate-400 hover:bg-slate-50"
                    }`}
                  >
                    <GripVertical aria-hidden="true" className="hidden size-4 shrink-0 text-slate-400 sm:block" />
                    <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{name}</span>
                    {checked ? <Check aria-hidden="true" className="size-4 shrink-0 text-brand-700" /> : null}
                  </div>
                );
              })}
            </div>
          )}
        </div>
        <div className="min-w-0">
          <p className="mb-1 text-xs font-medium text-slate-500">Selected</p>
          <div
            data-testid="instrument-drop-zone"
            onDragOver={(event) => {
              if (!accepts(event)) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = "copy";
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={onDrop}
            className={`flex min-h-[4.25rem] items-center rounded-md border-2 border-dashed px-3 py-2 text-sm transition-colors ${
              over ? "border-brand-600 bg-brand-50" : dragging ? "border-brand-300 bg-white" : invalid ? "border-red-300 bg-white" : "border-slate-200 bg-slate-50"
            }`}
          >
            {value ? (
              <span className="inline-flex min-w-0 max-w-full items-center gap-1 rounded-full border border-brand-200 bg-white py-1 pl-3 pr-1 font-medium text-slate-900">
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  {value}
                  {unlisted ? <span className="ml-1 text-xs font-normal text-slate-500">(not in the list)</span> : null}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    onChange("");
                    // The button goes away with the choice: keep focus in the picker.
                    if (status === "ready") focusFirst();
                  }}
                  aria-label={`Remove ${value}`}
                  className="inline-flex size-7 shrink-0 items-center justify-center rounded-full text-slate-500 hover:bg-slate-100 hover:text-slate-800 focus-visible:outline-2 focus-visible:outline-brand-600"
                >
                  <X aria-hidden="true" className="size-4" />
                </button>
              </span>
            ) : (
              <span className="text-slate-500">
                <span className="sm:hidden">Tap an instrument</span>
                <span className="hidden sm:inline">Drag or click an instrument</span>
              </span>
            )}
          </div>
        </div>
      </div>
      {invalid ? (
        <p id={errorId} className="mt-1.5 text-xs text-red-600">
          {errors?.join(" ")}
        </p>
      ) : null}
    </div>
  );
}
