"use client";

import type { JsonObject, JsonValue } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { Input } from "@/app/components/ui";

export function asObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

export function inputValue(value: unknown, fallback: string | number = ""): string | number {
  if (typeof value === "string" || typeof value === "number") return value;
  if (typeof value === "boolean") return String(value);
  return fallback;
}

/** Editable key/value pairs over a JSON object — an operation's headers and
 *  query string, and the userdata a test run or test call starts from.
 *
 *  `className` carries the spacing because the callers sit in different boxes:
 *  inside an operation card the rows bleed to the card's edge, and in the test
 *  panel and the test-call modal they do not. */
export function KVRows({
  obj,
  onChange,
  valuePlaceholder,
  empty,
  disabled,
  className = "px-4 pb-2.5",
  emptyClassName = "px-4 pb-2.5",
}: {
  obj: unknown;
  onChange: (obj: JsonObject) => void;
  valuePlaceholder?: string;
  /** Omit where the surrounding copy already says what an empty list means —
   *  two muted lines saying the same thing read as clutter. */
  empty?: string;
  disabled?: boolean;
  className?: string;
  emptyClassName?: string;
}): JSX.Element | null {
  const entries = Object.entries(asObject(obj));
  if (entries.length === 0) {
    if (!empty) return null;
    return <div className={cn("text-[13px] leading-5 text-muted", emptyClassName)}>{empty}</div>;
  }
  function update(index: number, key: string, value: string): void {
    onChange(Object.fromEntries(
      entries.map(([k, v], i) => (i === index ? [key, value] : [k, v])),
    ) as JsonObject);
  }
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {entries.map(([k, v], i) => (
        <div key={i} className="grid grid-cols-[minmax(0,0.8fr)_minmax(0,1.4fr)_28px] items-center gap-2">
          <Input
            value={k}
            placeholder="key"
            onChange={(e) => update(i, e.target.value, String(inputValue(v)))}
            aria-label={`Key ${i + 1}`}
            disabled={disabled}
            className="min-h-9 py-1.5 font-mono text-[13px]"
          />
          <Input
            value={inputValue(v)}
            placeholder={valuePlaceholder || "value"}
            onChange={(e) => update(i, k, e.target.value)}
            aria-label={`Value ${i + 1}`}
            disabled={disabled}
            className="min-h-9 py-1.5 text-[13px]"
          />
          <button
            type="button"
            onClick={() => onChange(Object.fromEntries(entries.filter((_, j) => j !== i)) as JsonObject)}
            aria-label={`Remove ${k || `row ${i + 1}`}`}
            disabled={disabled}
            className="grid h-7 w-7 place-items-center justify-self-end rounded-lg text-muted transition-colors hover:bg-danger/[0.06] hover:text-danger disabled:pointer-events-none disabled:opacity-40"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
              <path d="m3 3 6 6M9 3 3 9" />
            </svg>
          </button>
        </div>
      ))}
    </div>
  );
}
