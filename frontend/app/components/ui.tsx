"use client";
import Link from "next/link";
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/cn";

/* talqing UI primitives — "pristine instrument".
   One shared presentational vocabulary used by every surface (landing, auth,
   dashboard). No semantic CSS classes, no static inline styles. The handful of
   runtime-computed bindings that remain as `style` (Select popup position) are
   layout, not styling. */

/* ============================================================
   Button
   ============================================================ */
export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "md" | "sm";

const BTN_BASE =
  "inline-flex items-center justify-center gap-2 rounded-[10px] border font-medium whitespace-nowrap transition-[background-color,border-color,color,box-shadow,transform] duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10 disabled:opacity-40 disabled:pointer-events-none";
const BTN_SIZE: Record<ButtonSize, string> = {
  md: "min-h-9 px-3 py-0 text-[14px]",
  sm: "min-h-8 px-2.5 py-0 text-[13px]",
};
const BTN_VARIANT: Record<ButtonVariant, string> = {
  primary: "border-transparent bg-ink text-white shadow-none hover:bg-ink-hover",
  secondary: "border-line-2 bg-white text-ink shadow-none hover:bg-subtle",
  ghost: "border-transparent bg-transparent text-ink-soft hover:bg-hover hover:text-ink-hover",
  danger: "border-line-2 bg-white text-danger shadow-none hover:border-danger/25 hover:bg-danger/[0.05]",
};

/** Class string for a button-styled element (use with <Link>/<a>). */
export function btn(variant: ButtonVariant = "primary", size: ButtonSize = "md"): string {
  return cn(BTN_BASE, BTN_SIZE[size], BTN_VARIANT[variant]);
}

export function Button({
  variant = "primary",
  size = "md",
  className,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
}) {
  return <button className={cn(BTN_BASE, BTN_SIZE[size], BTN_VARIANT[variant], className)} {...rest} />;
}

/* ============================================================
   Form controls
   ============================================================ */
/* `aria-invalid` is the whole invalid state: setting it is what marks a field,
   and the ring follows. One rule here rather than a red class threaded through
   every caller — a field that announces itself invalid to a screen reader and
   looks fine to everyone else is the wrong half of the job. */
export const FIELD =
  "min-h-10 w-full rounded-[10px] border border-line-2 bg-white px-3 py-2 text-[14px] leading-5 text-ink placeholder:text-placeholder shadow-none transition-[border-color,box-shadow,background-color] hover:border-line-strong focus:outline-none focus:border-ink focus:ring-2 focus:ring-ink/10 disabled:opacity-50 disabled:hover:border-line-2 aria-[invalid=true]:border-danger aria-[invalid=true]:hover:border-danger aria-[invalid=true]:focus:border-danger aria-[invalid=true]:focus:ring-danger/15";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...rest }, ref) => <input ref={ref} className={cn(FIELD, className)} {...rest} />,
);
Input.displayName = "Input";

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement> & { autoGrow?: boolean }
>(({ className, autoGrow, ...rest }, ref) => {
  const el = useRef<HTMLTextAreaElement | null>(null);

  // Long-form fields (a system prompt, a tool description) are written and
  // re-read whole, and a fixed viewport onto them means editing line 40 blind.
  // `scrollHeight` after a reset to `auto` is the only number that knows how the
  // text actually wrapped; the border sits outside it under border-box, hence
  // the offset/client difference. min-h still sets the empty-state floor.
  useLayoutEffect(() => {
    if (!autoGrow || !el.current) return;
    const node = el.current;
    node.style.height = "auto";
    node.style.height = `${node.scrollHeight + node.offsetHeight - node.clientHeight}px`;
  }, [autoGrow, rest.value, rest.defaultValue]);

  return (
    <textarea
      ref={(node) => {
        el.current = node;
        if (typeof ref === "function") ref(node);
        else if (ref) ref.current = node;
      }}
      className={cn(
        FIELD,
        "min-h-[120px] leading-5",
        autoGrow ? "resize-none overflow-hidden" : "resize-y",
        className,
      )}
      {...rest}
    />
  );
});
Textarea.displayName = "Textarea";

export function Label({
  className,
  children,
  optional,
  ...rest
}: React.LabelHTMLAttributes<HTMLLabelElement> & { optional?: boolean }) {
  return (
    <label className={cn("text-[14px] font-semibold leading-5 text-ink", className)} {...rest}>
      {children}
      {optional && <span className="ml-1 font-normal text-faint">(optional)</span>}
    </label>
  );
}

export function Field({
  label,
  htmlFor,
  hint,
  className,
  children,
}: {
  label?: React.ReactNode;
  htmlFor?: string;
  hint?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {label && <Label htmlFor={htmlFor}>{label}</Label>}
      {children}
      {hint && <p className="text-[13px] leading-5 text-muted">{hint}</p>}
    </div>
  );
}

/* ============================================================
   Select — custom dropdown (popup portaled to <body>)
   Reads <option> children, emits onChange({ target: { value } }) — a string, or
   with `multiple` the list of checked values.
   ============================================================ */
type Opt = {
  value: string;
  label: string;
  disabled?: boolean;
  logoUrl?: string;
  /** From `title` on the `<option>`. Shown as a tooltip on hover and read as the
   *  option's description — for choices whose names do not explain themselves. */
  hint?: string;
  /** The enclosing `<optgroup label>`, when there is one. Where a value comes
   *  from can be the most important thing about it — the email batch's column
   *  pickers offer the same name from your CSV and from the task's output — so
   *  the popup keeps the heading rather than flattening the list. */
  group?: string;
  /** From `data-detail` on the `<option>`. A muted second line under the label,
   *  for the facts that decide a choice rather than describe it — a model's
   *  context window and price. Unlike `hint` it is always visible, because a
   *  tooltip is no use when the reader is comparing rows. */
  detail?: string;
  /** From `data-exclusive`, with `multiple`: checking it clears every other
   *  value, and checking any other clears it — an "Automatic" row, say, without
   *  a second control beside the list. */
  exclusive?: boolean;
  /** From `data-checked`, with `multiple`: shown checked without being part of
   *  the value, for a row another choice already implies. Pair it with
   *  `disabled`, since unchecking it could not mean anything. */
  checked?: boolean;
};

/** The 18px logo slot. Kept even when there is no logo (or the one there fails
 *  to load), so a list mixing both keeps every label on one left edge rather
 *  than jumping each logo-less row 26px left — and a dead favicon never shows a
 *  broken-image glyph. */
function OptionLogo({ url }: { url?: string }) {
  return (
    <span className="h-[18px] w-[18px] flex-none" aria-hidden="true">
      {url && (
        <img
          className="h-full w-full rounded object-contain"
          src={url}
          alt=""
          onError={(e) => {
            e.currentTarget.style.visibility = "hidden";
          }}
        />
      )}
    </span>
  );
}

/** `BoxCheckbox`'s box, drawn rather than interactive: inside a listbox the row
 *  is the option, and a nested button would be a second, unannounced control. */
function CheckMark({ checked }: { checked: boolean }) {
  return (
    <span className={cn(CHECK_BOX, checked && "bg-ink")} aria-hidden="true">
      {checked && <CheckGlyph />}
    </span>
  );
}

function nodeText(children: React.ReactNode): string {
  if (children == null || children === false) return "";
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(nodeText).join("");
  if (React.isValidElement(children)) return nodeText((children.props as any).children);
  return "";
}

function extractOptions(children: React.ReactNode, group?: string): Opt[] {
  const out: Opt[] = [];
  React.Children.forEach(children, (child) => {
    if (!React.isValidElement(child)) return;
    if (child.type === React.Fragment) {
      out.push(...extractOptions((child.props as any).children, group));
      return;
    }
    if (child.type === "optgroup") {
      const props: any = child.props || {};
      out.push(...extractOptions(props.children, props.label ? String(props.label) : group));
      return;
    }
    if (child.type === "option") {
      const props: any = child.props || {};
      const label = nodeText(props.children);
      const value = props.value !== undefined ? String(props.value) : label;
      out.push({
        value,
        label,
        disabled: !!props.disabled,
        logoUrl: props["data-logo-url"] ? String(props["data-logo-url"]) : undefined,
        hint: props.title ? String(props.title) : undefined,
        detail: props["data-detail"] ? String(props["data-detail"]) : undefined,
        exclusive: !!props["data-exclusive"],
        checked: !!props["data-checked"],
        group,
      });
    }
  });
  return out;
}

/* Keyboard contract follows the ARIA combobox/listbox pattern, because a
   dropdown that only answers to a mouse locks keyboard and screen-reader users
   out of every model, voice, and language choice in the product.

   Focus stays on the trigger the whole time — the listbox is referenced with
   aria-activedescendant rather than focused — so Escape, Tab and type-ahead
   need no focus juggling and the popup can live in a portal. */
let selectInstanceId = 0;

type SelectProps = {
  children: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
  disabled?: boolean;
  title?: string;
  /** Adds a filter box to the popup. Worth it past ~20 options, where
   *  type-ahead alone means knowing how the label starts. */
  searchable?: boolean;
  /** Hands the filter box to the caller: the popup stops filtering its own
   *  children and reports what was typed instead, so the options can come from
   *  a server. Implies `searchable`. This exists because one provider's model
   *  list is ~260 entries the browser never holds — everything else about the
   *  control (the popup, the keyboard contract, the portal) is the same, and a
   *  second dropdown implementation for that one case would be a worse product
   *  and a worse codebase. */
  onSearch?: (query: string) => void;
  /** With `onSearch`, whether a fetch is in flight — an empty list then reads as
   *  "still looking" rather than "nothing matches", and a full one grows a
   *  "Loading…" row while the next page arrives. */
  busy?: boolean;
  /** Called when the popup is scrolled to its last option, so a caller serving
   *  options from a server can append the next page. Without it a list of 260
   *  would be a list of the first 30 with no way to reach the rest. */
  onScrollEnd?: () => void;
  /** What the trigger shows when `value` matches none of the options. With
   *  server-side options that is the normal state on first paint: the saved
   *  value is real and simply has not been fetched. Without this the control
   *  would say "Select…" about a model the agent is already running. */
  valueLabel?: string;
  /** Drops the field chrome (border, background, min-height, full width) so the
   *  trigger can sit inside another control — a prefix on an input, say. The
   *  popup, keyboard handling and type-ahead are unchanged, which is the whole
   *  point of asking for it here rather than hand-rolling a second dropdown. */
  inline?: boolean;
  /** Extra classes for the trigger button. Type and colour live here. */
  triggerClassName?: string;
  /** Extra classes for the popup. The popup is portaled, so a font set on the
   *  trigger does not reach it — set it in both or the two disagree. */
  popupClassName?: string;
  "aria-label"?: string;
  /* Declared, because TypeScript does not check hyphenated JSX attributes — an
     undeclared `aria-invalid` here would be silently dropped rather than
     rejected, and the field would look fine while announcing itself invalid. */
  "aria-invalid"?: boolean;
};

type SelectChoice =
  | { multiple?: false; value: any; onChange: (e: { target: { value: string } }) => void }
  | {
      /** Checkboxes rather than one choice. The popup stays open while rows are
       *  toggled, and rows never reorder when checked — a row jumping under the
       *  cursor is worse than a checked one further down. */
      multiple: true;
      value: string[];
      onChange: (e: { target: { value: string[] } }) => void;
    };

export function Select(props: SelectChoice & SelectProps) {
  const {
    value,
    children,
    className,
    style,
    disabled,
    title,
    searchable,
    onSearch,
    busy,
    onScrollEnd,
    valueLabel,
    inline,
    triggerClassName,
    popupClassName,
    "aria-label": ariaLabel,
    "aria-invalid": ariaInvalid,
  } = props;
  const all = extractOptions(children);
  const cur = String(value ?? "");
  const chosen = props.multiple ? new Set(props.value) : null;
  const isChecked = (o: Opt) => (chosen ? chosen.has(o.value) || !!o.checked : o.value === cur);
  const checkedOpts = chosen ? all.filter((o) => chosen.has(o.value)) : [];
  const selected = chosen ? checkedOpts[0] : all.find((o) => o.value === cur);
  const hasLogos = all.some((o) => o.logoUrl);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const withSearch = searchable || !!onSearch;
  const needle = withSearch && !onSearch ? query.trim().toLowerCase() : "";
  const opts = needle ? all.filter((o) => o.label.toLowerCase().includes(needle)) : all;
  // popup is portaled + fixed-positioned so overflow:hidden ancestors can't clip it.
  // position/left/top are runtime-measured → stay as inline style (layout, not styling).
  const [pos, setPos] = useState<React.CSSProperties | null>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const ref = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const typeAhead = useRef({ query: "", at: 0 });
  const idRef = useRef<string>();
  if (!idRef.current) idRef.current = `uisel-${++selectInstanceId}`;
  const listboxId = `${idRef.current}-listbox`;
  const optionId = (i: number) => `${idRef.current}-opt-${i}`;
  // Where the popup opens: the current choice, or the first checked row.
  const startIndex = () => opts.findIndex((o) => (chosen ? chosen.has(o.value) : o.value === cur));

  const firstEnabled = (from: number, step: number) => {
    for (let i = from; i >= 0 && i < opts.length; i += step) {
      if (!opts[i].disabled) return i;
    }
    return -1;
  };

  function openPopup(startAt: number) {
    if (disabled) return;
    const r = ref.current!.getBoundingClientRect();
    const spaceBelow = window.innerHeight - r.bottom;
    const openUp = spaceBelow < 260 && r.top > spaceBelow;
    setPos({
      position: "fixed",
      left: r.left,
      minWidth: r.width,
      ...(openUp ? { bottom: window.innerHeight - r.top + 6 } : { top: r.bottom + 6 }),
    });
    setQuery("");
    onSearch?.("");
    setActiveIndex(startAt);
    setOpen(true);
  }

  function closePopup() {
    setOpen(false);
    setQuery("");
    setActiveIndex(-1);
  }

  function toggle() {
    if (disabled) return;
    if (open) closePopup();
    else openPopup(startIndex());
  }

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      const t = e.target as Node;
      if (ref.current?.contains(t) || popRef.current?.contains(t)) return;
      // The press that dismisses the popup does nothing else — otherwise
      // clicking a Modal's backdrop to shed the popup takes the dialog with it.
      // Only mousedown is stopped, so a click on whatever was pressed still
      // lands on the next press.
      e.stopPropagation();
      closePopup();
    }
    function onAway(e: Event) {
      if (popRef.current?.contains(e.target as Node)) return;
      closePopup();
    }
    // Capture, not bubble: a Select can sit inside a Modal, and the dialog stops
    // mousedown from propagating so a press inside it never closes the dialog.
    // On the bubble phase that also means the press never reaches this listener
    // and the popup stays open over the dialog. Capture runs document → target,
    // before anything can stop it.
    document.addEventListener("mousedown", onDoc, true);
    window.addEventListener("scroll", onAway, true);
    window.addEventListener("resize", onAway);
    return () => {
      document.removeEventListener("mousedown", onDoc, true);
      window.removeEventListener("scroll", onAway, true);
      window.removeEventListener("resize", onAway);
    };
  }, [open]);

  useEffect(() => {
    if (open && withSearch) searchRef.current?.focus();
  }, [open, withSearch]);

  // Keep the active option in view while arrowing through a long list.
  useEffect(() => {
    if (!open || activeIndex < 0) return;
    popRef.current
      ?.querySelector(`[data-index="${activeIndex}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [open, activeIndex]);

  function choose(o: Opt) {
    if (o.disabled) return;
    if (!props.multiple) {
      props.onChange({ target: { value: o.value } });
      closePopup();
      return;
    }
    const exclusive = new Set(all.filter((x) => x.exclusive).map((x) => x.value));
    const next = o.exclusive
      ? props.value.includes(o.value)
        ? []
        : [o.value]
      : props.value.includes(o.value)
        ? props.value.filter((v) => v !== o.value)
        : [...props.value.filter((v) => !exclusive.has(v)), o.value];
    props.onChange({ target: { value: next } });
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (disabled) return;
    const step = (delta: number) => {
      e.preventDefault();
      if (!open) {
        openPopup(firstEnabled(delta > 0 ? 0 : opts.length - 1, delta > 0 ? 1 : -1));
        return;
      }
      const next = firstEnabled(activeIndex + delta, delta);
      if (next !== -1) setActiveIndex(next);
    };

    switch (e.key) {
      case "ArrowDown":
        return step(1);
      case "ArrowUp":
        return step(-1);
      case "Home":
        if (!open) return;
        e.preventDefault();
        return setActiveIndex(firstEnabled(0, 1));
      case "End":
        if (!open) return;
        e.preventDefault();
        return setActiveIndex(firstEnabled(opts.length - 1, -1));
      case "Enter":
      case " ":
        e.preventDefault();
        if (!open) return openPopup(startIndex());
        if (activeIndex >= 0) choose(opts[activeIndex]);
        return;
      case "Escape":
        if (!open) return;
        e.preventDefault();
        // Dismiss the popup only. Without this the keydown reaches the Modal
        // listening on document and the dialog closes underneath it too.
        e.stopPropagation();
        return closePopup();
      case "Tab":
        // Tab commits nothing and moves on, like a native select.
        if (open) closePopup();
        return;
      default:
        break;
    }

    // Type-ahead: printable keys jump to the next option starting with what
    // was typed. Keystrokes within a second of each other build one query.
    // The filter box takes those keystrokes instead when there is one.
    if (withSearch || e.key.length !== 1 || e.metaKey || e.ctrlKey || e.altKey) return;
    const now = Date.now();
    const state = typeAhead.current;
    state.query = now - state.at < 1000 ? state.query + e.key : e.key;
    state.at = now;
    const query = state.query.toLowerCase();
    const from = (open && activeIndex >= 0 ? activeIndex : startIndex()) + 1;
    const order = [...opts.slice(from), ...opts.slice(0, from)];
    const hit = order.find((o) => !o.disabled && o.label.toLowerCase().startsWith(query));
    if (!hit) return;
    e.preventDefault();
    const index = opts.indexOf(hit);
    if (open) setActiveIndex(index);
    else openPopup(index);
  }

  return (
    <div
      className={cn("relative", inline ? "w-auto" : "w-full", className)}
      style={style}
      ref={ref}
      title={title}
    >
      <button
        type="button"
        className={cn(
          inline
            ? "rounded-[9px] text-[14px] leading-5 text-ink transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10 disabled:opacity-50"
            : cn(
                FIELD,
                "data-[open=true]:border-ink data-[open=true]:ring-2 data-[open=true]:ring-ink/10",
              ),
          "flex items-center justify-between gap-2 text-left",
          disabled && "opacity-50",
          triggerClassName,
        )}
        disabled={disabled}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-activedescendant={open && activeIndex >= 0 ? optionId(activeIndex) : undefined}
        aria-label={ariaLabel}
        aria-invalid={ariaInvalid}
        data-open={open}
        onClick={toggle}
        onKeyDown={onKeyDown}
      >
        <span className="flex min-w-0 items-center gap-2 overflow-hidden">
          {selected?.logoUrl && <OptionLogo url={selected.logoUrl} />}
          <span className="overflow-hidden text-ellipsis whitespace-nowrap">
            {selected?.label || (!chosen && cur ? valueLabel : "") || "Select…"}
          </span>
          {checkedOpts.length > 1 && (
            <span className="flex-none text-muted">+{checkedOpts.length - 1}</span>
          )}
        </span>
        <svg className={cn("h-3.5 w-3.5 flex-none text-muted transition-transform", open && "rotate-180")} viewBox="0 0 12 12" aria-hidden>
          <path d="M2.5 4.5L6 8l3.5-3.5" stroke="currentColor" strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open &&
        pos &&
        createPortal(
          <div
            className={cn(
              // Above Modal's z-[120]: a select opened inside a dialog must
              // paint over it, and a select outside one can never be open while
              // a dialog is up, so there is nothing for it to obscure.
              "scroll-thin z-[130] max-h-[300px] w-max max-w-[340px] overflow-y-auto rounded-[10px] border border-line-2 bg-surface p-1 shadow-pop animate-slide-up",
              popupClassName,
            )}
            style={pos}
            role="listbox"
            aria-multiselectable={chosen ? true : undefined}
            id={listboxId}
            ref={popRef}
            onWheel={(e) => e.stopPropagation()}
            onScroll={(e) => {
              if (!onScrollEnd) return;
              const el = e.currentTarget;
              // One row's worth of slack, so the next page starts arriving just
              // before the reader runs out of list rather than after.
              if (el.scrollTop + el.clientHeight >= el.scrollHeight - 48) onScrollEnd();
            }}
          >
            {withSearch && (
              <input
                ref={searchRef}
                className="mb-1 w-full rounded-lg border border-line-2 bg-surface px-2 py-1.5 text-[14px] leading-5 text-ink outline-none placeholder:text-muted focus:border-ink"
                placeholder="Search…"
                value={query}
                aria-label="Filter options"
                onChange={(e) => {
                  setQuery(e.target.value);
                  setActiveIndex(-1);
                  onSearch?.(e.target.value);
                }}
                onKeyDown={onKeyDown}
              />
            )}
            {withSearch && opts.length === 0 && (
              <div className="px-2 py-1.5 text-[14px] leading-5 text-muted">
                {busy ? "Searching…" : "No matches"}
              </div>
            )}
            {opts.map((o, i) => (
              /* The heading rides with the option rather than being an entry of
                 its own, so `i` stays the option's index — which is what
                 activeIndex, type-ahead and aria-activedescendant all count in. */
              <React.Fragment key={`${o.value}-${i}`}>
                {o.group && o.group !== opts[i - 1]?.group && (
                  <div
                    role="presentation"
                    className={cn(
                      "px-2 pb-1 pt-2 text-[11.5px] font-semibold uppercase tracking-[0.07em] text-faint",
                      i === 0 && "pt-1",
                    )}
                  >
                    {o.group}
                  </div>
                )}
                <div
                  id={optionId(i)}
                  role="option"
                  data-index={i}
                  aria-selected={isChecked(o)}
                  aria-disabled={o.disabled || undefined}
                  aria-describedby={o.hint ? `${optionId(i)}-hint` : undefined}
                  className={cn(
                    "flex min-h-8 w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1 text-left text-[14px] font-medium leading-5 text-ink",
                    !chosen && o.value === cur && "bg-subtle",
                    i === activeIndex && "bg-hover",
                    o.disabled && "cursor-default opacity-50",
                  )}
                  onMouseEnter={() => !o.disabled && setActiveIndex(i)}
                  onClick={() => choose(o)}
                >
                  {chosen && <CheckMark checked={isChecked(o)} />}
                  {hasLogos && <OptionLogo url={o.logoUrl} />}
                  {o.hint ? (
                    // The row itself is the hover target, so the bubble follows the
                    // whole option rather than only the width of its text.
                    <Tooltip label={o.hint} focusable={false} className="min-w-0 flex-1">
                      <span className="overflow-hidden text-ellipsis">{o.label}</span>
                    </Tooltip>
                  ) : o.detail ? (
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="overflow-hidden text-ellipsis">{o.label}</span>
                      <span className="overflow-hidden text-ellipsis text-[12.5px] font-normal leading-4 text-muted">
                        {o.detail}
                      </span>
                    </span>
                  ) : (
                    <span className="overflow-hidden text-ellipsis">{o.label}</span>
                  )}
                  {o.hint && (
                    <span id={`${optionId(i)}-hint`} className="sr-only">
                      {o.hint}
                    </span>
                  )}
                </div>
              </React.Fragment>
            ))}
            {busy && opts.length > 0 && (
              <div className="px-2 py-1.5 text-[13px] leading-5 text-muted">Loading…</div>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}

/* ============================================================
   Menu — overflow ("⋯") actions, popup portaled to <body>
   ============================================================ */
export type MenuItem = {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
};

/* Same portal + fixed-position approach as Select, for the same reason: a row
   in an `overflow:hidden` table would otherwise clip its own menu. Focus stays
   on the trigger and the list is referenced by aria-activedescendant, so
   Escape and Tab need no focus juggling. */
let menuInstanceId = 0;

export function Menu({
  items,
  label = "More actions",
  className,
}: {
  items: MenuItem[];
  label?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<React.CSSProperties | null>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const ref = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const idRef = useRef<string>();
  if (!idRef.current) idRef.current = `uimenu-${++menuInstanceId}`;
  const menuId = `${idRef.current}-menu`;
  const itemId = (i: number) => `${idRef.current}-item-${i}`;

  const firstEnabled = (from: number, step: number) => {
    for (let i = from; i >= 0 && i < items.length; i += step) {
      if (!items[i].disabled) return i;
    }
    return -1;
  };

  function openPopup(startIndex: number) {
    const r = ref.current!.getBoundingClientRect();
    const spaceBelow = window.innerHeight - r.bottom;
    const openUp = spaceBelow < 220 && r.top > spaceBelow;
    setPos({
      position: "fixed",
      // Right-aligned: the trigger sits at the end of a row, so a
      // left-aligned popup would hang off the viewport.
      left: Math.max(8, r.right - 200),
      minWidth: 200,
      ...(openUp ? { bottom: window.innerHeight - r.top + 6 } : { top: r.bottom + 6 }),
    });
    setActiveIndex(startIndex);
    setOpen(true);
  }

  function closePopup() {
    setOpen(false);
    setActiveIndex(-1);
  }

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      const t = e.target as Node;
      if (ref.current?.contains(t) || popRef.current?.contains(t)) return;
      // Consumes the dismissing press, as Select's popup does.
      e.stopPropagation();
      closePopup();
    }
    function onAway(e: Event) {
      if (popRef.current?.contains(e.target as Node)) return;
      closePopup();
    }
    // Capture, for the same reason as Select's popup above.
    document.addEventListener("mousedown", onDoc, true);
    window.addEventListener("scroll", onAway, true);
    window.addEventListener("resize", onAway);
    return () => {
      document.removeEventListener("mousedown", onDoc, true);
      window.removeEventListener("scroll", onAway, true);
      window.removeEventListener("resize", onAway);
    };
  }, [open]);

  function choose(item: MenuItem) {
    if (item.disabled) return;
    closePopup();
    item.onSelect();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    const step = (delta: number) => {
      e.preventDefault();
      if (!open) {
        openPopup(firstEnabled(delta > 0 ? 0 : items.length - 1, delta > 0 ? 1 : -1));
        return;
      }
      const next = firstEnabled(activeIndex + delta, delta);
      if (next !== -1) setActiveIndex(next);
    };
    switch (e.key) {
      case "ArrowDown":
        return step(1);
      case "ArrowUp":
        return step(-1);
      case "Enter":
      case " ":
        e.preventDefault();
        if (!open) return openPopup(firstEnabled(0, 1));
        if (activeIndex >= 0) choose(items[activeIndex]);
        return;
      case "Escape":
        if (!open) return;
        e.preventDefault();
        // Dismiss the menu only, not a Modal it may be sitting inside.
        e.stopPropagation();
        return closePopup();
      case "Tab":
        if (open) closePopup();
        return;
      default:
        return;
    }
  }

  return (
    <div className={cn("relative", className)} ref={ref}>
      <button
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-activedescendant={open && activeIndex >= 0 ? itemId(activeIndex) : undefined}
        data-open={open}
        onClick={() => (open ? closePopup() : openPopup(firstEnabled(0, 1)))}
        onKeyDown={onKeyDown}
        className="flex h-8 w-8 items-center justify-center rounded-[10px] border border-transparent text-muted transition-colors hover:border-line-2 hover:bg-subtle hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10 data-[open=true]:border-line-2 data-[open=true]:bg-subtle data-[open=true]:text-ink"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
          <circle cx="3" cy="8" r="1.4" />
          <circle cx="8" cy="8" r="1.4" />
          <circle cx="13" cy="8" r="1.4" />
        </svg>
      </button>
      {open &&
        pos &&
        createPortal(
          <div
            // Above Modal's z-[120], for the same reason as Select's popup.
            className="z-[130] rounded-[10px] border border-line-2 bg-surface p-1 shadow-pop animate-slide-up"
            style={pos}
            role="menu"
            id={menuId}
            ref={popRef}
          >
            {items.map((item, i) => (
              <div
                key={item.label}
                id={itemId(i)}
                role="menuitem"
                aria-disabled={item.disabled || undefined}
                className={cn(
                  "flex min-h-8 cursor-pointer items-center rounded-lg px-2.5 py-1 text-[13.5px] font-medium leading-5",
                  item.danger ? "text-danger" : "text-ink",
                  i === activeIndex && (item.danger ? "bg-danger/[0.06]" : "bg-hover"),
                  item.disabled && "cursor-default opacity-40",
                )}
                onMouseEnter={() => !item.disabled && setActiveIndex(i)}
                onClick={() => choose(item)}
              >
                {item.label}
              </div>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}

/* ============================================================
   Badge
   ============================================================ */
export type BadgeVariant = "default" | "live" | "warn" | "info" | "danger";
const BADGE_VARIANT: Record<BadgeVariant, string> = {
  default: "border-line-2 bg-subtle text-ink-soft",
  live: "border-live/25 bg-live/[0.06] text-live",
  warn: "border-warn/30 bg-warn/[0.06] text-warn",
  info: "border-info/25 bg-info/[0.05] text-info",
  danger: "border-danger/25 bg-danger/[0.05] text-danger",
};

export function Badge({
  variant = "default",
  dot,
  title,
  className,
  children,
}: {
  variant?: BadgeVariant;
  dot?: boolean;
  title?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-[3px] text-[12px] font-medium leading-4 whitespace-nowrap",
        BADGE_VARIANT[variant],
        className,
      )}
    >
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

/* ============================================================
   Tooltip — hover/focus detail, popup portaled to <body>
   ============================================================ */

/* Portaled for the same reason as Select and Menu: the caller is often a cell
   inside an `overflow:hidden` table, which would clip an absolutely-positioned
   bubble. Opens on hover and on keyboard focus, and is wired up with
   aria-describedby so the text is not mouse-only. */
let tooltipInstanceId = 0;

export function Tooltip({
  label,
  children,
  className,
  style,
  focusable = true,
}: {
  label: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
  /** Set false where the trigger cannot own focus — inside a `role="option"`,
   *  say, whose listbox drives selection with aria-activedescendant. The caller
   *  is then responsible for describing the content some other way. */
  focusable?: boolean;
}) {
  const [pos, setPos] = useState<React.CSSProperties | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  const bubbleRef = useRef<HTMLSpanElement>(null);
  const idRef = useRef<string>();
  if (!idRef.current) idRef.current = `uitip-${++tooltipInstanceId}`;

  function show() {
    const r = ref.current!.getBoundingClientRect();
    const openDown = r.top < 90;
    setPos({
      position: "fixed",
      left: Math.min(Math.max(8, r.left + r.width / 2), window.innerWidth - 8),
      ...(openDown ? { top: r.bottom + 8 } : { bottom: window.innerHeight - r.top + 8 }),
    });
  }

  const hide = useCallback(() => setPos(null), []);

  // Focus alone is not intent. A dialog that sends focus to its first control on
  // open would otherwise pop that control's bubble the instant it appears, with
  // the pointer nowhere near it. :focus-visible is the browser's own answer to
  // "did a keyboard put focus here?", so keyboard users still get the text and
  // mouse users only get it by pointing.
  function showIfKeyboard(e: React.FocusEvent) {
    if (e.target instanceof Element && e.target.matches(":focus-visible")) show();
  }

  // The bubble is centred on its trigger, so near either edge half of it lands
  // off-screen. Nudge the rendered box back inside rather than re-rendering: the
  // width is only knowable once it has wrapped.
  useLayoutEffect(() => {
    const bubble = bubbleRef.current;
    if (!pos || !bubble) return;
    bubble.style.transform = "translateX(-50%)";
    const r = bubble.getBoundingClientRect();
    const dx = r.left < 8 ? 8 - r.left : r.right > window.innerWidth - 8 ? window.innerWidth - 8 - r.right : 0;
    if (dx) bubble.style.transform = `translateX(calc(-50% + ${Math.round(dx)}px))`;
  }, [pos]);

  useEffect(() => {
    if (!pos) return;
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, [pos, hide]);

  return (
    <>
      <span
        ref={ref}
        className={cn("inline-flex cursor-help", className)}
        style={style}
        tabIndex={focusable ? 0 : undefined}
        aria-describedby={focusable && pos ? idRef.current : undefined}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={showIfKeyboard}
        onBlur={hide}
      >
        {children}
      </span>
      {pos &&
        createPortal(
          <span
            ref={bubbleRef}
            id={idRef.current}
            role="tooltip"
            style={pos}
            // Topmost layer, above Modal's z-[120] and Select's z-[130]: a
            // tooltip can be triggered from inside either — an option hint is
            // rendered within the select popup itself — and it is
            // pointer-events-none and transient, so there is nothing it can
            // obscure by painting over everything.
            className="pointer-events-none z-[140] block w-max max-w-[320px] -translate-x-1/2 rounded-lg border border-line-strong bg-ink px-2.5 py-2 text-left text-[12px] font-medium leading-snug text-surface shadow-pop animate-fade-in [overflow-wrap:anywhere]"
          >
            {label}
          </span>,
          document.body,
        )}
    </>
  );
}

/* ============================================================
   Surfaces — Panel / Card / CardHead
   ============================================================ */
/* No `shadow-none` here: box-shadow already defaults to none, so the class only
   ever said out loud what was true anyway — and it outranked any shadow a caller
   passed in, because twMerge treats our custom `shadow-rest`/`shadow-pop` as
   shadow *colours* (they are not keys it knows) and so never collapses the pair. */
export const SURFACE = "rounded-xl border border-line-2 bg-white";

/* The floating-card gesture, opt-in on top of SURFACE: the card rests on a
   hairline shadow and lifts 1px under the cursor. It marks "the one you are
   pointing at" in a stack of like-shaped cards — the tool editor's operation
   tree and the agent editor's config sections — so a surface that stands alone
   on its page should not wear it.

   Deliberately at the edge of noticeable. It used to lift 2px onto `shadow-pop`,
   which is the drop shadow *floating* UI wears — a popover leaves the page, a
   card you are reading does not, and at editor length a stack of them bouncing
   that far reads as restlessness. */
export const LIFT_ON_HOVER =
  "shadow-rest transition-[transform,box-shadow,border-color] duration-150 ease-out " +
  "hover:-translate-y-px hover:border-line-strong hover:shadow-soft " +
  "motion-reduce:transition-none motion-reduce:hover:transform-none";

export function Panel({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn(SURFACE, "p-5", className)} {...rest}>
      {children}
    </div>
  );
}

/* Stage hues, for the headings that configure a billed stage of a call. Spelled
   out because Tailwind cannot see a class name assembled at runtime, and kept
   here so a stage heading is the same token as that stage's segment in the cost
   meter — that identity is the only reason colour appears in the editor at all.
   A hue means "a billed stage of a call": don't spend one on anything else. */
export type CardAccent = "stt" | "llm" | "tts" | "realtime" | "avatar" | "analysis";
export const STAGE_HUE: Record<CardAccent, string> = {
  stt: "bg-chart-stt",
  llm: "bg-chart-llm",
  tts: "bg-chart-tts",
  realtime: "bg-chart-realtime",
  avatar: "bg-chart-avatar",
  analysis: "bg-chart-analysis",
};

/* One column template for every stage band, so Provider and Model line up all
   the way down the Models card. Only the language model fills the third column
   (Thinking); on the other two it is there to hold the alignment — without it
   their Model select stretched 200px wider than the one above it. */
export const STAGE_GRID =
  "grid grid-cols-1 gap-3.5 md:grid-cols-[minmax(170px,220px)_minmax(220px,1fr)] " +
  "lg:grid-cols-[minmax(170px,220px)_minmax(220px,1fr)_minmax(150px,180px)]";

/** `STAGE_GRID` held at two columns, for a language model with a host picker:
 *  Provider | Model over Thinking | Hosts, so the model keeps its width and its
 *  hosts sit under it rather than squeezing a fourth column into the row. */
export const MODEL_HOSTS_GRID =
  "grid grid-cols-1 gap-3.5 md:grid-cols-[minmax(170px,220px)_minmax(220px,1fr)]";

export function CardHead({
  title,
  desc,
  children,
  className,
}: {
  title?: React.ReactNode;
  desc?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("-mx-5 -mt-5 mb-4 flex items-start gap-3 border-b border-line px-5 py-4", className)}>
      <div className="min-w-0">
        {title && (
          <div className="text-[16px] font-semibold leading-6 text-ink">{title}</div>
        )}
        {desc && <div className="mt-1 text-[13px] leading-5 text-muted">{desc}</div>}
      </div>
      <div className="flex-1" />
      {children && <div className="flex flex-none items-center gap-2">{children}</div>}
    </div>
  );
}

/* One section of an editor: a titled head, a hairline, then the fields. The
   editors are long, and this is what makes them scannable — every section is the
   same shape, so the head is where you look and the rule is where a section
   ends. The tool editor is built out of these; the agent editor is moving onto
   them section by section, replacing Panel + CardHead. */
export function SectionCard({
  id,
  title,
  helper,
  action,
  className,
  children,
}: {
  /** Scroll anchor, for an editor whose tab bar jumps between sections. */
  id?: string;
  title: string;
  helper?: React.ReactNode;
  action?: React.ReactNode;
  /** For a section whose body bleeds to the card edge and so has to be clipped
      to its rounded corners. Not the default — it would clip popups elsewhere. */
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className={cn(SURFACE, className)}>
      <div className="flex items-start gap-3 px-5 py-4">
        <div className="min-w-0 flex-1">
          <h2 className="text-[16px] font-semibold leading-6 text-ink">{title}</h2>
          {helper && <p className="mt-0.5 text-[13px] leading-5 text-muted">{helper}</p>}
        </div>
        {action}
      </div>
      <div className="border-t border-line px-5 py-4">
        <div className="flex flex-col gap-4">{children}</div>
      </div>
    </section>
  );
}

/* ============================================================
   Layout — Container / PageHead / SectionLabel / Row / Col / Spacer
   ============================================================ */
export function Container({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("w-full px-6 pb-24 pt-0", className)} {...rest}>
      {children}
    </div>
  );
}

export function PageHead({
  title,
  sub,
  eyebrow,
  actions,
  className,
}: {
  title: React.ReactNode;
  sub?: React.ReactNode;
  eyebrow?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mb-6 flex items-start gap-6 border-b border-line px-0 py-5", className)}>
      <div className="min-w-0">
        {eyebrow && (
          <div className="mb-1 text-[13px] font-medium leading-5 text-muted">{eyebrow}</div>
        )}
        <h1 className="text-[24px] font-semibold leading-8 text-ink">{title}</h1>
        {sub && <p className="mt-2 max-w-[80ch] text-[14px] leading-5 text-muted">{sub}</p>}
      </div>
      {actions && <div className="ml-auto flex flex-none items-center gap-2.5 pt-1">{actions}</div>}
    </div>
  );
}

export function SectionLabel({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mb-3 mt-8 flex items-center gap-3 first:mt-0", className)}>
      <span className="text-[14px] font-semibold leading-5 text-ink">{children}</span>
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}

/** Quieter kin of SectionLabel: a small muted caption over a panel, no rule. */
export function SectionEyebrow({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mb-2.5 text-[13px] font-medium leading-5 text-muted", className)}>
      {children}
    </div>
  );
}

export function Row({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("flex items-center gap-3", className)} {...rest}>
      {children}
    </div>
  );
}

export function Col({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("flex flex-col gap-2", className)} {...rest}>
      {children}
    </div>
  );
}

export function Spacer() {
  return <div className="flex-1" />;
}

/* ============================================================
   Chat — Transcript + ChatBubble (web call, web chat, copilot)
   ============================================================ */
export const Transcript = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  function Transcript({ className, children, ...rest }, ref) {
    return (
      <div
        ref={ref}
        className={cn("scroll-thin flex flex-col gap-2.5 overflow-y-auto", className)}
        {...rest}
      >
        {children}
      </div>
    );
  },
);

export function ChatBubble({
  role,
  who,
  children,
  className,
}: {
  role: "user" | "agent";
  who?: string;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "max-w-[82%] whitespace-pre-wrap rounded-xl px-3.5 py-2.5 text-[14px] leading-5",
        role === "user"
          ? "self-end rounded-br-md bg-ink text-white"
          : "self-start rounded-bl-md border border-line-2 bg-white text-ink",
        className,
      )}
    >
      {who && (
        <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-faint">
          {who}
        </div>
      )}
      {children}
    </div>
  );
}

/* ============================================================
   Scroll lock — shared by every overlay that covers the page
   ============================================================ */

/* An overlay that leaves the page behind it scrollable lets a scroll gesture
   move the wrong thing, so each one locks the body while it is open. The lock
   is counted rather than saved-and-restored, because saving the value you found
   is only correct for one overlay at a time: a dialog that opens a second dialog
   records "hidden" as the state to go back to, and React runs a parent's
   unmount cleanup BEFORE its child's, so closing both at once restores "" and
   then puts "hidden" back. That is how the page under a closed dialog ended up
   frozen — and a route change on the way out carried the frozen body to the
   next page, where nothing was left on screen to explain it.

   A count has no order to get wrong: the last overlay out unlocks. */
let scrollLocks = 0;

function useScrollLock() {
  useEffect(() => {
    if (scrollLocks++ === 0) document.body.style.overflow = "hidden";
    return () => {
      if (--scrollLocks === 0) document.body.style.overflow = "";
    };
  }, []);
}

/* ============================================================
   Images — transcript attachments, composer tray, lightbox
   ============================================================ */

/** One image on a conversation item, as the API returns it. Structural subset of
    the SDK's `ConversationAttachment`, so any surface holding one can pass it. */
export type AttachmentImage = {
  id: string;
  mime_type: string;
  width: number;
  height: number;
  filename?: string | null;
  url: string;
  url_expires_at: string;
};

/** Images on a transcript turn: capped, click to see full size.
 *
 *  `aspect-ratio` comes from the stored dimensions, so the row occupies its
 *  final shape before a single byte arrives and nothing under it jumps when the
 *  images land. A presigned URL lives an hour, so a page left open longer gets a
 *  Reload rather than a broken-image icon — that is what `url_expires_at` is
 *  returned for. */
export function AttachmentImages({
  images,
  onReload,
  className,
}: {
  images: AttachmentImage[];
  /** Re-fetch the item so its links are signed again. */
  onReload?: () => void;
  className?: string;
}) {
  const [zoomed, setZoomed] = useState<AttachmentImage | null>(null);
  const [broken, setBroken] = useState<Record<string, true>>({});
  if (images.length === 0) return null;
  return (
    <div className={cn("flex flex-wrap gap-2", className)}>
      {images.map((image) =>
        broken[image.id] ? (
          <div
            key={image.id}
            className="flex h-[120px] w-[160px] flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-line-strong bg-subtle px-3 text-center"
          >
            {/* `url_expires_at` is returned so this can tell the two apart. A
                link that has run out reloads and works; anything else is a
                bucket or network problem, and calling that "expired" would send
                the reader looking in the wrong place. */}
            <span className="text-[11.5px] leading-4 text-muted">
              {Date.now() > Date.parse(image.url_expires_at)
                ? "This link has expired"
                : "This image didn't load"}
            </span>
            {onReload && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setBroken((current) => {
                    const next = { ...current };
                    delete next[image.id];
                    return next;
                  });
                  onReload();
                }}
              >
                Reload
              </Button>
            )}
          </div>
        ) : (
          <button
            key={image.id}
            type="button"
            onClick={() => setZoomed(image)}
            title="See full size"
            className="group overflow-hidden rounded-lg border border-line-2 bg-subtle transition-[border-color,box-shadow] hover:border-line-strong focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10"
          >
            <img
              src={image.url}
              alt={image.filename || "attached image"}
              width={image.width}
              height={image.height}
              onError={() => setBroken((current) => ({ ...current, [image.id]: true }))}
              className="block max-h-[240px] max-w-[240px] object-contain transition-transform duration-200 group-hover:scale-[1.01]"
              style={{ aspectRatio: `${image.width} / ${image.height}` }}
            />
          </button>
        ),
      )}
      {zoomed && (
        <ImageLightbox
          src={zoomed.url}
          alt={zoomed.filename || "attached image"}
          onClose={() => setZoomed(null)}
        />
      )}
    </div>
  );
}

/** One image, as large as the viewport allows. Escape or a click anywhere closes it. */
export function ImageLightbox({
  src,
  alt,
  onClose,
}: {
  src: string;
  alt: string;
  onClose: () => void;
}) {
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);
  useScrollLock();
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onCloseRef.current();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  return createPortal(
    <div
      className="fixed inset-0 z-[130] grid place-items-center bg-ink/70 p-6 backdrop-blur-[3px] animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-label={alt}
      onMouseDown={onClose}
    >
      <img
        src={src}
        alt={alt}
        className="max-h-full max-w-full rounded-lg object-contain shadow-lift"
        onMouseDown={(event) => event.stopPropagation()}
      />
    </div>,
    document.body,
  );
}

export type TrayImage = {
  id: string;
  name: string;
  /** A `data:` or object URL for the thumbnail. */
  previewUrl: string;
};

/** Images staged in a composer: attached, not yet sent.
 *
 *  Only a composer has these. On a call an image is sent the instant it is
 *  picked, so it is a message from that moment and belongs in the transcript
 *  with the rest of what was said — not in a strip above it. */
export function ImageTray({
  images,
  onRemove,
  className,
}: {
  images: TrayImage[];
  onRemove?: (id: string) => void;
  className?: string;
}) {
  if (images.length === 0) return null;
  return (
    <div className={cn("flex flex-wrap gap-2", className)}>
      {images.map((image) => (
        <div key={image.id} className="relative">
          <div className="h-14 w-14 overflow-hidden rounded-lg border border-line-2 bg-subtle">
            <img
              src={image.previewUrl}
              alt={image.name}
              title={image.name}
              className="h-full w-full object-cover"
            />
          </div>
          {onRemove && (
            <button
              type="button"
              aria-label={`Remove ${image.name}`}
              onClick={() => onRemove(image.id)}
              className="absolute -right-1.5 -top-1.5 grid h-5 w-5 place-items-center rounded-full border border-line-2 bg-surface text-ink-soft shadow-sm transition-colors hover:bg-subtle hover:text-ink"
            >
              <svg width="9" height="9" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M3 3l8 8M11 3l-8 8" /></svg>
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

/* ============================================================
   EmptyState / Skeleton / ListSkeleton
   ============================================================ */
/* Full-bleed, so it lines up with the panels above and below it. Its content is
   centred and capped, which is what keeps the text readable in a wide box. */
export function EmptyState({
  title,
  body,
  cta,
  icon,
}: {
  title: string;
  body: string;
  cta?: React.ReactNode;
  icon?: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-dashed border-line-strong bg-white px-6 py-14 text-center">
      <div className="mb-4 inline-flex h-11 w-11 items-center justify-center rounded-xl border border-line-2 bg-subtle text-ink-soft">
        {icon || (
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L4 16.8 7.2 20l5.3-5.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.2-.4-.4-2.2 2.6-2.6Z" />
          </svg>
        )}
      </div>
      <h3 className="mb-2 text-[18px] font-semibold leading-6 text-ink">{title}</h3>
      <p className="mx-auto mb-5 max-w-[52ch] text-[14px] leading-5 text-muted">{body}</p>
      {cta}
    </div>
  );
}

/** Nothing to attach yet, inside an editor card. An empty list is a place to
 *  start, not a note. */
export function NothingToAttach({
  what,
  href,
  action,
  children,
}: {
  what: string;
  /** Where the thing is built. Omitted when the card head already offers it. */
  href?: string;
  action?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-start gap-1 rounded-xl border border-dashed border-line-strong bg-canvas px-5 py-5">
      <p className="text-[14px] font-semibold leading-5 text-ink">No {what} yet</p>
      <p className="max-w-[62ch] text-[13px] leading-5 text-muted">{children}</p>
      {href && action && (
        <Link href={href} className={cn(btn("secondary", "sm"), "mt-2.5")}>
          {action}
        </Link>
      )}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "animate-shimmer rounded-md bg-[length:200%_100%] bg-gradient-to-r from-subtle via-line to-subtle",
        className,
      )}
    />
  );
}

export function ListSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="overflow-hidden rounded-xl border border-line-2 bg-white shadow-none">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3.5 border-b border-line px-4 py-3.5 last:border-b-0">
          <Skeleton className="h-9 w-9 rounded-lg" />
          <Skeleton className="h-4 w-[150px]" />
          <Skeleton className="h-3.5 w-[220px]" />
          <div className="flex-1" />
          <Skeleton className="h-5 w-14 rounded-full" />
        </div>
      ))}
    </div>
  );
}

/* ============================================================
   SessionStatus — the live-state pill on the test call / chat surfaces
   ============================================================ */
/** `idle` waiting on the user, `busy` mid-handshake, `live` connected. */
export function SessionStatus({
  tone = "idle",
  children,
}: {
  tone?: "idle" | "busy" | "live";
  children: React.ReactNode;
}) {
  return (
    // role=status so a connection that comes up (or drops) is announced rather
    // than only shown — the user is looking at their mic, not at this pill.
    <span
      role="status"
      className={cn(
        "inline-flex items-center gap-2 rounded-full border px-2.5 py-1 text-[12.5px] font-medium",
        tone === "live"
          ? "border-live/25 bg-live/[0.08] text-live"
          : tone === "busy"
            ? "border-warn/25 bg-warn/[0.08] text-warn"
            : "border-line-2 bg-surface text-muted",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-1.5 w-1.5 flex-none rounded-full",
          tone === "live" ? "animate-pulse bg-live" : tone === "busy" ? "animate-pulse bg-warn" : "bg-line-strong",
        )}
      />
      {children}
    </span>
  );
}

/* ============================================================
   HelpDot — inline tooltip (real DOM, no ::after)
   ============================================================ */
export function HelpDot({ label, className }: { label: React.ReactNode; className?: string }) {
  return (
    <Tooltip label={label} className={cn("group", className)}>
      <span className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-line-2 bg-surface text-[10px] font-bold leading-none text-faint transition-colors group-hover:border-info/40 group-hover:bg-info/10 group-hover:text-info group-focus-visible:border-info/40 group-focus-visible:bg-info/10 group-focus-visible:text-info">
        i
      </span>
    </Tooltip>
  );
}

/* ============================================================
   RowRemove — the × that takes one row out of a repeating editor
   ============================================================ */
/** Take this row away.
 *
 *  The app's answer to "remove one of these" everywhere a list is edited: a
 *  variable, a key/value pair, a recipient, an output field. Quiet until it is
 *  hovered, then danger — a destructive control should not shout while you are
 *  reading past it, and should be unmistakable once you are on it.
 *
 *  It exists because the markup was pasted ten times and had already drifted:
 *  some copies carried a focus ring and no disabled state, others the reverse,
 *  others neither. Both belong — the ring is how it is reachable without a
 *  mouse, and `disabled` is how an editor says "not the last one". */
export function RowRemove({
  onClick,
  ariaLabel,
  disabled,
  title,
  className,
}: {
  onClick: () => void;
  ariaLabel: string;
  disabled?: boolean;
  /** Why it is unavailable, when it is — a disabled control with no reason is
   *  just a dead one. */
  title?: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      title={title}
      className={cn(
        "grid h-7 w-7 place-items-center justify-self-end rounded-lg text-muted transition-colors",
        "hover:bg-danger/[0.06] hover:text-danger",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/15",
        "disabled:pointer-events-none disabled:opacity-40",
        className,
      )}
    >
      <svg
        className="h-3.5 w-3.5"
        viewBox="0 0 12 12"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        aria-hidden
      >
        <path d="m3 3 6 6M9 3 3 9" />
      </svg>
    </button>
  );
}

/* ============================================================
   BoxCheckbox — the square checkbox used across the app
   ============================================================ */
/** The box and tick every checkbox in the app wears — `BoxCheckbox`, and the
 *  drawn marks in a multi-choice `Select`. */
const CHECK_BOX =
  "grid h-5 w-5 flex-none place-items-center rounded-[6px] border border-line-2 bg-surface text-white shadow-[0_1px_3px_rgba(0,0,0,0.10),0_1px_2px_-1px_rgba(0,0,0,0.10)]";

function CheckGlyph() {
  return (
    <svg className="h-3.5 w-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m3.5 8.5 3 3 6-7" />
    </svg>
  );
}

export function BoxCheckbox({
  checked,
  onChange,
  disabled,
  ariaLabel,
  className,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  ariaLabel: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        CHECK_BOX,
        "transition-colors hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-ink/10 disabled:cursor-not-allowed disabled:opacity-50",
        checked && "bg-ink",
        className,
      )}
    >
      {checked && <CheckGlyph />}
    </button>
  );
}

/* ============================================================
   Modal / ValidationModal
   ============================================================ */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Modal({
  title,
  sub,
  onClose,
  children,
  footer,
  width = "max-w-[620px]",
}: {
  title?: React.ReactNode;
  sub?: React.ReactNode;
  onClose: () => void;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  width?: string;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useScrollLock();

  // Escape must call the current handler, but everything else below is
  // once-per-dialog work. Held in a ref so the effect can drop `onClose` from
  // its deps: every caller passes an inline arrow, so depending on it re-ran
  // this on each render — and a form that re-renders per keystroke had the
  // caret yanked back to its first field on every character typed.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    // Send focus into the dialog and put it back where it came from on close,
    // so keyboard users are not dropped at the top of the page afterwards.
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    // A dialog built around typed input should open with the caret in the first
    // such field, whatever buttons happen to precede it in the DOM — a "+ add a
    // row" button above the rows it adds to would otherwise take the focus *and*
    // the Enter key. Only fields you type or pick into: a checkbox or a slider
    // is a setting the dialog offers, not the thing it is asking for. Failing
    // that, skip the corner ✕, which is first in the DOM and last in intent —
    // every dialog here puts its safe action (Cancel / Keep editing) before its
    // destructive one, so the next focusable is a sound landing.
    const focusable = Array.from(dialog?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    // A dialog that only shows something names its own landing, so the focus
    // does not fall on a link inside what it shows.
    const target =
      focusable.find((el) => el.hasAttribute("data-dialog-autofocus")) ??
      focusable.find((el) =>
        el.matches('textarea, select, input:not([type="checkbox"], [type="radio"], [type="range"])'),
      ) ??
      focusable.find((el) => !el.hasAttribute("data-dialog-dismiss")) ??
      focusable[0];
    // A named landing may be the footer of a tall dialog: take the focus there
    // without scrolling the dialog down to it.
    (target ?? dialog)?.focus({ preventScroll: target?.hasAttribute("data-dialog-autofocus") });

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab" || !dialogRef.current) return;
      // Keep Tab inside the dialog — otherwise focus walks into the inert page.
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previouslyFocused?.focus?.();
    };
  }, []);

  return createPortal(
    <div
      className="fixed inset-0 z-[120] grid place-items-center bg-ink/35 p-6 backdrop-blur-[3px] animate-fade-in"
      role="presentation"
      onMouseDown={onClose}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className={cn(
          "max-h-[calc(100vh-48px)] w-full overflow-auto rounded-2xl border border-line bg-surface shadow-lift focus:outline-none",
          width,
        )}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        // Closing is driven by mousedown on the backdrop, so a press that starts
        // inside must not bubble — otherwise a text selection dragged past the
        // edge of the dialog would discard it.
        onMouseDown={(e) => e.stopPropagation()}
      >
        {(title || sub) && (
          <div className="mb-4 flex items-start gap-4 border-b border-line px-6 py-5">
            <div className="min-w-0">
              {title && <div className="font-display text-[18px] font-semibold tracking-tight text-ink">{title}</div>}
              {sub && <div className="mt-1 text-[13px] leading-relaxed text-muted">{sub}</div>}
            </div>
            <button
              type="button"
              aria-label="Close"
              data-dialog-dismiss
              onClick={onClose}
              className="ml-auto flex h-[30px] w-[30px] flex-none items-center justify-center rounded-lg border border-line-2 bg-surface text-ink-soft transition-colors hover:bg-subtle hover:text-ink"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><path d="M3 3l8 8M11 3l-8 8" /></svg>
            </button>
          </div>
        )}
        <div className="px-6">{children}</div>
        {footer && (
          <div className="mx-6 mb-6 mt-4 flex justify-end gap-2.5 border-t border-line pt-4">{footer}</div>
        )}
      </div>
    </div>,
    document.body,
  );
}

export function ValidationModal({
  title,
  errors = [],
  warnings = [],
  sub,
  confirmLabel = "Continue",
  cancelLabel = "Cancel",
  errorAction,
  onConfirm,
  onClose,
}: {
  title: string;
  errors?: string[];
  warnings?: string[];
  /** Overrides the line under the title. The default speaks of publishing,
      which is right wherever this gates a publish and wrong everywhere else. */
  sub?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** What fixes this error, for the errors that can be fixed from here — a
      missing BYOK key is the one that can. Return null to leave the row as the
      sentence it is. */
  errorAction?: (error: string) => React.ReactNode;
  onConfirm?: () => void;
  onClose: () => void;
}) {
  const hasErrors = errors.length > 0;
  return (
    <Modal
      title={title}
      sub={
        sub ??
        (hasErrors ? "Fix these issues before publishing." : "Review these warnings before publishing.")
      }
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {hasErrors ? "Close" : cancelLabel}
          </Button>
          {!hasErrors && onConfirm && (
            <Button variant="primary" onClick={onConfirm}>
              {confirmLabel}
            </Button>
          )}
        </>
      }
    >
      <div className="pb-6">
        {hasErrors && (
          <div className="mb-3 grid gap-2 rounded-lg border border-danger/25 bg-danger/[0.05] p-3.5">
            <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-danger">Errors</div>
            {errors.map((er, i) => (
              <div key={i} className="flex items-start gap-3">
                <div className="min-w-0 flex-1 self-center text-[13px] leading-snug text-danger [overflow-wrap:anywhere]">
                  {er}
                </div>
                {errorAction?.(er)}
              </div>
            ))}
          </div>
        )}
        {warnings.length > 0 && (
          <div className="grid gap-2 rounded-lg border border-warn/25 bg-warn/[0.05] p-3.5">
            <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-warn">Warnings</div>
            {warnings.map((w, i) => (
              <div key={i} className="text-[13px] leading-snug text-ink-soft [overflow-wrap:anywhere]">
                {w}
              </div>
            ))}
          </div>
        )}
      </div>
    </Modal>
  );
}

/** Confirmation shown when leaving an editor would discard a draft. */
export function UnsavedChangesModal({
  sub,
  onKeepEditing,
  onDiscard,
}: {
  sub: string;
  onKeepEditing: () => void;
  onDiscard: () => void;
}) {
  return (
    <Modal
      title="Discard unsaved changes?"
      sub={sub}
      onClose={onKeepEditing}
      width="max-w-[480px]"
      footer={
        <>
          <Button variant="secondary" onClick={onKeepEditing}>
            Keep editing
          </Button>
          <Button variant="danger" onClick={onDiscard}>
            Leave without saving
          </Button>
        </>
      }
    />
  );
}

/* ============================================================
   Toast / Toaster — global, bottom-right
   ============================================================ */
export type ToastKind = "default" | "ok" | "err";
type Toast = { id: number; kind: ToastKind; msg: string };
const ToastCtx = createContext<(t: { kind?: ToastKind; msg: string }) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

const TOAST_KIND: Record<ToastKind, string> = {
  default: "bg-ink text-surface border-transparent",
  ok: "border-live/20 bg-live/[0.08] text-live",
  err: "border-danger/20 bg-danger/[0.06] text-danger",
};

export function Toaster({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const idRef = useRef(0);
  const timers = useRef<number[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((x) => x.id !== id));
  }, []);

  const push = useCallback(
    (t: { kind?: ToastKind; msg: string }) => {
      const id = ++idRef.current;
      setToasts((prev) => [...prev, { id, kind: t.kind || "default", msg: t.msg }]);
      timers.current.push(window.setTimeout(() => dismiss(id), 4000));
    },
    [dismiss],
  );

  useEffect(() => () => timers.current.forEach(window.clearTimeout), []);

  return (
    <ToastCtx.Provider value={push}>
      {children}
      {/* Toasts carry the only confirmation that a save or publish worked, so
          they have to reach a screen reader too. Errors interrupt; the rest
          wait their turn. */}
      <div className="pointer-events-none fixed bottom-6 right-6 z-[130] flex flex-col gap-2.5">
        {toasts.map((t) => (
          <div
            key={t.id}
            role={t.kind === "err" ? "alert" : "status"}
            aria-live={t.kind === "err" ? "assertive" : "polite"}
            className={cn(
              "pointer-events-auto flex max-w-[420px] items-center gap-2.5 rounded-xl border px-4 py-3 text-[13.5px] font-medium shadow-pop animate-slide-up",
              TOAST_KIND[t.kind],
            )}
          >
            <span className="min-w-0 flex-1">{t.msg}</span>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={() => dismiss(t.id)}
              className="-mr-1 grid h-5 w-5 flex-none place-items-center rounded opacity-60 transition-opacity hover:opacity-100"
            >
              <svg width="10" height="10" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden><path d="M3 3l8 8M11 3l-8 8" /></svg>
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* ============================================================
   Stats — the KPI row above a list
   ============================================================ */
/* `detail` is where the denominator goes. Most figures worth a card are a rate
   over a subset — judged calls, priced calls, sessions carrying a metrics bag —
   and a rate whose denominator is invisible is a rate nobody can trust. `help`
   is for the rule behind the number, which is longer than a card line. */
export function StatCard({
  label,
  value,
  detail,
  help,
  icon,
}: {
  label: string;
  value: React.ReactNode;
  detail?: React.ReactNode;
  help?: React.ReactNode;
  icon?: React.ReactNode;
}) {
  return (
    <div className="min-w-0 rounded-xl border border-line bg-white p-4 transition-colors hover:border-line-strong">
      <div className="flex items-center gap-1.5">
        {icon && <span className="mr-0.5 text-faint">{icon}</span>}
        <span className="truncate text-[13px] font-medium leading-5 text-muted">{label}</span>
        {help && <HelpDot label={help} />}
      </div>
      <div className="mt-2 truncate text-[22px] font-semibold leading-7 text-ink tabular-nums">
        {value}
      </div>
      {detail && (
        <div
          className="mt-1 truncate text-[13px] leading-5 text-faint"
          // Only a plain string can be a tooltip; a node would stringify to
          // "[object Object]" and put that in the browser's own chrome.
          title={typeof detail === "string" ? detail : undefined}
        >
          {detail}
        </div>
      )}
    </div>
  );
}

export function Stats({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("grid grid-cols-2 gap-3.5 lg:grid-cols-4", className)}>{children}</div>
  );
}

/* ============================================================
   Ledger strip — the row of figures that sits above a list
   ============================================================ */
/* One cell of a strip: divided by hairlines rather than gaps, so the figures
   read as one instrument panel instead of four floating cards. Meant to be
   dropped into a bordered flex/grid parent (see the Agents and Phone numbers
   pages), which is what draws the strip's own outline. */
export function Figure({
  label,
  value,
  detail,
}: {
  label: string;
  value: React.ReactNode;
  detail?: React.ReactNode;
}) {
  return (
    <div className="min-w-0 border-b border-line px-4 py-3 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0">
      <div className="text-[12px] font-medium leading-4 text-muted">{label}</div>
      <div className="mt-1 truncate font-display text-[19px] font-semibold leading-6 tracking-tight text-ink tabular-nums">
        {value}
      </div>
      {detail && <div className="mt-0.5 truncate text-[11.5px] leading-4 text-faint">{detail}</div>}
    </div>
  );
}

/* ============================================================
   SHEET — the look of a batch's rows, as a spreadsheet
   ============================================================ */
/* The email and WhatsApp batch tables are one kind of screen: a list's own
   columns beside what was sent, scrolled sideways. These are the classes both
   are built from, so one cannot drift from the other. The columns a row is
   sent by are `pinned`: set in ink, and headed by what they are for. Row
   actions are ghost buttons (`btn("ghost", "sm")`). */
export const SHEET = {
  wrap: "scroll-thin min-w-0 overflow-x-auto rounded-xl border border-line-2 bg-white",
  table: "w-full border-collapse text-[13px]",
  thead: "bg-canvas/60",
  headRow: "border-b border-line text-left text-[12px] font-medium text-muted",
  th: "px-3 py-2 font-medium",
  thNumber: "w-10 py-2 pl-4 pr-2 font-medium",
  thPinned: "px-3 py-2 font-medium text-ink",
  thData: "px-3 py-2 font-mono font-medium text-ink-soft",
  /** The list's own name for a pinned column, beside what it is for. */
  thName: "font-mono text-[11.5px] font-normal text-faint",
  row: "h-12 border-b border-line last:border-0",
  td: "px-3 py-1.5",
  tdNumber: "py-1.5 pl-4 pr-2 font-mono text-[12px] tabular-nums text-faint",
  tdPinned: "max-w-[240px] truncate px-3 py-1.5 font-medium text-ink",
  tdData: "max-w-[240px] truncate px-3 py-1.5 text-ink-soft",
} as const;

/* ============================================================
   FigureTabs — the counts of a list, as the filter over it
   ============================================================ */
/* A strip of figures above a list and a row of filter tabs with the same
   numbers on them are one control drawn twice. Here each figure is the tab:
   pressing it shows its rows. `children` is a band under the figures, for the
   bar the counts add up to. Laid out for eight figures at full width. */
export function FigureTabs<T extends string>({
  value,
  onChange,
  options,
  children,
  className,
}: {
  value: T;
  onChange: (next: T) => void;
  options: { value: T; label: string; count: number; detail?: string | null }[];
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("overflow-hidden rounded-xl border border-line-2 bg-white", className)}>
      <div className="grid grid-cols-2 gap-px bg-line sm:grid-cols-4 xl:grid-cols-8">
        {options.map((option) => {
          const active = option.value === value;
          return (
            <button
              key={option.value}
              type="button"
              aria-pressed={active}
              onClick={() => onChange(option.value)}
              className={cn(
                "min-w-0 bg-white px-4 py-3 text-left transition-colors focus:outline-none focus-visible:bg-subtle",
                active ? "shadow-[inset_0_-2px_0_theme(colors.ink)]" : "hover:bg-canvas",
              )}
            >
              <div className={cn("truncate text-[12px] font-medium leading-4", active ? "text-ink" : "text-muted")}>
                {option.label}
              </div>
              <div className="mt-1 truncate font-display text-[19px] font-semibold leading-6 tracking-tight text-ink tabular-nums">
                {option.count.toLocaleString()}
              </div>
              <div className="mt-0.5 h-4 truncate text-[11.5px] leading-4 text-faint">{option.detail}</div>
            </button>
          );
        })}
      </div>
      {children && <div className="border-t border-line px-4 py-2.5">{children}</div>}
    </section>
  );
}

/* ============================================================
   Segment — a small exclusive filter control
   ============================================================ */
/* Counts live on the control rather than beside it: the point of a filter chip
   is knowing what it will leave you with before you press it. */
export function Segment<T extends string>({
  value,
  onChange,
  options,
  equal,
  className,
}: {
  value: T;
  onChange: (next: T) => void;
  /** `count` is optional: a segmented control that switches between two views
   *  has nothing to count, and a `0` there would read as "empty". */
  options: { value: T; label: string; count?: number }[];
  /** Fill the width, every option the same share of it. For a control that
   *  heads a panel rather than sitting inline in a toolbar, where options of
   *  different widths read as different weights. */
  equal?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "inline-flex items-center gap-0.5 rounded-[10px] border border-line-2 bg-subtle p-0.5",
        equal && "flex w-full",
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(option.value)}
            className={cn(
              "inline-flex min-h-8 items-center gap-1.5 rounded-lg px-2.5 text-[13px] font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10",
              equal && "flex-1 basis-0 justify-center",
              active ? "bg-white text-ink shadow-rest" : "text-muted hover:text-ink",
            )}
          >
            {option.label}
            {option.count !== undefined && (
              <span
                className={cn(
                  "font-mono text-[11.5px] tabular-nums",
                  active ? "text-faint" : "text-placeholder",
                )}
              >
                {option.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ============================================================
   Pager — numbered pages under a list whose length is known
   ============================================================ */
/* The first and last page are always there, with the current one and its
   neighbours between them; a run left out is an ellipsis. `page` counts from 0. */
export function Pager({
  page,
  pageSize,
  total,
  onPage,
  className,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (page: number) => void;
  className?: string;
}) {
  const pages = Math.ceil(total / pageSize);
  if (total === 0) return null;
  const shown = [...new Set([0, page - 1, page, page + 1, pages - 1])]
    .filter((p) => p >= 0 && p < pages)
    .sort((a, b) => a - b);
  const cell =
    "grid h-8 min-w-8 place-items-center rounded-lg px-2 text-[13px] font-medium tabular-nums transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10 disabled:pointer-events-none disabled:opacity-40";
  const arrow = (to: number, label: string, path: string) => (
    <button
      type="button"
      aria-label={label}
      disabled={to < 0 || to >= pages}
      onClick={() => onPage(to)}
      className={cn(cell, "text-muted hover:bg-hover hover:text-ink")}
    >
      <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d={path} />
      </svg>
    </button>
  );
  return (
    <div className={cn("flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-[13px] text-muted", className)}>
      <span className="tabular-nums">
        Rows {(page * pageSize + 1).toLocaleString()}–{Math.min(total, (page + 1) * pageSize).toLocaleString()} of{" "}
        {total.toLocaleString()}
      </span>
      {pages > 1 && (
        <nav aria-label="Pages" className="flex items-center gap-0.5">
          {arrow(page - 1, "Previous page", "m15 6-6 6 6 6")}
          {shown.map((p, i) => (
            <React.Fragment key={p}>
              {i > 0 && p - shown[i - 1] > 1 && (
                <span aria-hidden className="w-6 text-center text-placeholder">
                  …
                </span>
              )}
              <button
                type="button"
                aria-current={p === page ? "page" : undefined}
                onClick={() => onPage(p)}
                className={cn(
                  cell,
                  p === page ? "bg-subtle text-ink ring-1 ring-inset ring-line-2" : "text-muted hover:bg-hover hover:text-ink",
                )}
              >
                {p + 1}
              </button>
            </React.Fragment>
          ))}
          {arrow(page + 1, "Next page", "m9 6 6 6-6 6")}
        </nav>
      )}
    </div>
  );
}

/* ============================================================
   CopyButton — copy one value, say so for a moment
   ============================================================ */
/* `value` may be a thunk so a caller can serialize a large payload only when
   the button is actually pressed. */
export function CopyButton({
  value,
  label,
  ariaLabel,
  className,
}: {
  value: string | (() => string);
  label?: string;
  ariaLabel?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(t);
  }, [copied]);
  return (
    <button
      type="button"
      aria-label={ariaLabel || label || "Copy"}
      onClick={(e) => {
        e.stopPropagation();
        void navigator.clipboard
          .writeText(typeof value === "function" ? value() : value)
          .then(() => setCopied(true));
      }}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border border-line-2 bg-white px-1.5 py-1 text-[12px] font-medium text-muted transition-colors hover:border-line-strong hover:text-ink",
        className,
      )}
    >
      {copied ? (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M20 6 9 17l-5-5" /></svg>
      ) : (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></svg>
      )}
      {label && <span>{copied ? "Copied" : label}</span>}
    </button>
  );
}

/* ============================================================
   Disclosure — a <details> section with a summary row and a meta slot
   ============================================================ */
/* Native <details> so the content is findable by in-page search and works
   without JS; `open` is uncontrolled on purpose. */
export function Disclosure({
  summary,
  meta,
  defaultOpen,
  className,
  children,
}: {
  summary: React.ReactNode;
  meta?: React.ReactNode;
  defaultOpen?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <details open={defaultOpen} className={cn("group border-b border-line last:border-b-0", className)}>
      <summary className="flex cursor-pointer list-none items-center gap-3 px-5 py-3.5 transition-colors hover:bg-hover [&::-webkit-details-marker]:hidden">
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
          className="flex-none text-faint transition-transform group-open:rotate-90"
        >
          <path d="m9 18 6-6-6-6" />
        </svg>
        <span className="min-w-0 flex-1 text-[13.5px] font-medium text-ink">{summary}</span>
        {meta && <span className="flex-none text-[12px] text-faint">{meta}</span>}
      </summary>
      <div className="px-5 pb-5 pt-1">{children}</div>
    </details>
  );
}

/* ============================================================
   DescriptionList — labelled facts, optionally copyable
   ============================================================ */
export function DescriptionList({
  columns = 3,
  className,
  children,
}: {
  columns?: 2 | 3;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <dl
      className={cn(
        "grid gap-x-6 gap-y-3.5 sm:grid-cols-2",
        columns === 3 && "lg:grid-cols-3",
        className,
      )}
    >
      {children}
    </dl>
  );
}

export function DescriptionItem({
  term,
  mono,
  copyValue,
  children,
}: {
  term: string;
  mono?: boolean;
  copyValue?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[12px] font-medium leading-4 text-muted">{term}</dt>
      <dd
        className={cn(
          "mt-1 flex min-w-0 items-center gap-1.5 text-[13.5px] leading-5 text-ink",
          mono && "font-mono text-[12.5px]",
        )}
      >
        <span className="min-w-0 truncate">{children}</span>
        {copyValue && (
          <CopyButton value={copyValue} ariaLabel={`Copy ${term}`} className="flex-none" />
        )}
      </dd>
    </div>
  );
}
