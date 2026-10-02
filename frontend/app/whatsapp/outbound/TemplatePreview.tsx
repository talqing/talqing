"use client";

import { useState } from "react";
import type { WhatsAppTemplate, WhatsAppTemplateButton } from "@talqing/sdk";
import { Tooltip } from "@/app/components/ui";
import { cn } from "@/lib/cn";

const PLACEHOLDER = /\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g;

/* WhatsApp's own formatting, which recipients see rendered: *bold*, _italic_,
   ~strike~. A marker counts only at a word boundary and hugging its text, so
   the underscore inside `{{first_name}}` is not the start of italics. */
const FORMATTING = /(?<![\p{L}\p{N}_])([*_~])(?=\S)([^\n]*?\S)\1(?![\p{L}\p{N}_])/gu;
const STYLE = { "*": "strong", _: "em", "~": "s" } as const;

type Values = Record<string, string> | undefined;

/** Placeholders in one run of text: filled and underlined, flagged when its
 *  value is missing, or left visible when there are no values to fill it with. */
function withValues(text: string, values: Values, key: number): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(PLACEHOLDER)) {
    const index = match.index ?? 0;
    out.push(text.slice(last, index));
    const value = values?.[match[1]]?.trim();
    out.push(
      !values ? (
        <span key={`${key}-${index}`} className="rounded bg-white/70 px-1 font-mono text-[12px] text-ink">
          {match[0]}
        </span>
      ) : value ? (
        <span key={`${key}-${index}`} className="font-medium text-ink underline decoration-live/40 underline-offset-2">
          {value}
        </span>
      ) : (
        <span key={`${key}-${index}`} className="rounded bg-warn/10 px-1 font-mono text-[12px] text-warn">
          {match[0]}
        </span>
      ),
    );
    last = index + match[0].length;
  }
  out.push(text.slice(last));
  return out;
}

/** A message body as WhatsApp renders it: its *bold*, _italic_ and ~strike~
 *  applied, and each `{{placeholder}}` handled as `withValues` does. */
export function whatsAppText(body: string, values?: Record<string, string>): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  let last = 0;
  for (const match of body.matchAll(FORMATTING)) {
    const index = match.index ?? 0;
    parts.push(...withValues(body.slice(last, index), values, last));
    const Tag = STYLE[match[1] as keyof typeof STYLE];
    parts.push(<Tag key={index}>{withValues(match[2], values, index)}</Tag>);
    last = index + match[0].length;
  }
  parts.push(...withValues(body.slice(last), values, last));
  return parts;
}

/** A link or code with this row's values in it; a missing one stays `{{name}}`. */
const filled = (text: string, values: Values) =>
  text.replace(PLACEHOLDER, (whole, name: string) => values?.[name]?.trim() || whole);

/** The last path segment of a URL, as the name of the file it points at. */
function fileName(url: string): string {
  const path = url.split(/[?#]/)[0];
  return decodeURIComponent(path.slice(path.lastIndexOf("/") + 1)) || url;
}

/** One template as the recipient will see it: header, body, footer and buttons
 *  in one bubble, with this row's values marked.
 *
 *  Values are shown in place and underlined, so a reader can tell the wording
 *  Meta approved from what the list filled in; a value that is missing stays a
 *  visible `{{1}}` in the warning colour rather than disappearing. */
export function TemplatePreview({
  template,
  values,
  className,
  bodyClassName,
}: {
  template: WhatsAppTemplate;
  /** Omit to show the template itself, placeholders and all. */
  values?: Record<string, string>;
  className?: string;
  /** For the text between the header and the buttons — a height cap, say. */
  bodyClassName?: string;
}) {
  const { header, footer, buttons } = template;
  return (
    <div className={cn("rounded-xl bg-subtle p-3", className)}>
      <div className="max-w-[360px] overflow-hidden rounded-lg rounded-tl-none border border-live/15 bg-live/[0.07] text-[13px] leading-5 text-ink-soft shadow-[0_1px_0_rgba(15,15,16,0.04)]">
        {header && header.type !== "text" && header.url && (
          <div className="p-1 pb-0">
            {/* Remounted per URL, so a failed load does not outlive a new value. */}
            <HeaderMedia key={filled(header.url, values)} type={header.type} url={filled(header.url, values)} />
          </div>
        )}
        <div className={cn("whitespace-pre-wrap break-words px-3 py-2", bodyClassName)}>
          {header?.type === "text" && header.text && (
            <div className="mb-1 text-[13.5px] font-semibold text-ink">{withValues(header.text, values, 0)}</div>
          )}
          {whatsAppText(template.body, values)}
        </div>
        {footer && <div className="break-words px-3 pb-2 text-[12px] leading-4 text-faint">{footer}</div>}
        {buttons.map((button) => (
          <ButtonRow key={`${button.type}-${button.text}`} button={button} values={values} />
        ))}
      </div>
    </div>
  );
}

function HeaderMedia({ type, url }: { type: "image" | "video" | "document"; url: string }) {
  const [failed, setFailed] = useState(false);
  if (type === "image" && !failed)
    return (
      <img
        src={url}
        alt=""
        onError={() => setFailed(true)}
        className="max-h-[240px] w-full rounded-md object-cover"
      />
    );
  if (type === "video" && !failed)
    return (
      <video
        src={url}
        controls
        preload="metadata"
        onError={() => setFailed(true)}
        className="max-h-[240px] w-full rounded-md bg-ink/[0.06]"
      />
    );
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="flex items-center gap-2.5 rounded-md bg-white/70 px-2.5 py-2 text-ink-soft transition-colors hover:text-ink"
    >
      <FileIcon />
      <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{fileName(url)}</span>
    </a>
  );
}

function ButtonRow({ button, values }: { button: WhatsAppTemplateButton; values: Values }) {
  const row = "flex min-h-9 items-center justify-center gap-1.5 border-t border-live/15 px-3 py-1.5 text-[13px] font-medium text-ink";
  if (button.type !== "url" || !button.url)
    return (
      <div className={row}>
        <ButtonIcon type={button.type} />
        <span className="truncate">{button.text}</span>
      </div>
    );
  const url = filled(button.url, values);
  return (
    <Tooltip label={url} className="block">
      <a href={url} target="_blank" rel="noreferrer" className={cn(row, "transition-colors hover:bg-live/[0.06]")}>
        <ButtonIcon type="url" />
        <span className="truncate">{button.text}</span>
      </a>
    </Tooltip>
  );
}

const ICON = {
  className: "h-[15px] w-[15px] flex-none text-muted",
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

function ButtonIcon({ type }: { type: WhatsAppTemplateButton["type"] }) {
  if (type === "url")
    return (
      <svg {...ICON}>
        <path d="M14 5h5v5" />
        <path d="M19 5 11 13" />
        <path d="M18 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h4" />
      </svg>
    );
  if (type === "quick_reply")
    return (
      <svg {...ICON}>
        <path d="M9 14 4 9l5-5" />
        <path d="M4 9h10a6 6 0 0 1 6 6v4" />
      </svg>
    );
  if (type === "copy_code")
    return (
      <svg {...ICON}>
        <rect x="9" y="9" width="11" height="11" rx="2" />
        <path d="M5 15V6a2 2 0 0 1 2-2h9" />
      </svg>
    );
  return (
    <svg {...ICON}>
      <path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2Z" />
    </svg>
  );
}

const FileIcon = () => (
  <svg {...ICON} className="h-[18px] w-[18px] flex-none text-muted">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" />
    <path d="M14 3v5h5" />
  </svg>
);
