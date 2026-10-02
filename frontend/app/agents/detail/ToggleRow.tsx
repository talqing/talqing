"use client";
import { BoxCheckbox, HelpDot } from "@/app/components/ui";
import { cn } from "@/lib/cn";

/** One switch on its own row: the label carries a field's weight, and everything
    it needs to explain sits on the help dot at the right edge, where every other
    row's dot is too.

    `disabledReason` greys the row and states, under it, what would have to
    change first. Shown rather than hidden when the setting is a real capability
    of some models and not others — the author is choosing between those models
    right above, and a control that vanishes tells them nothing about why.

    The rule under each row separates one *thing* from the next, so a group of
    switches that all belong to the same thing — screen share's Enabled and
    Record — passes `divider={false}` and reads as one block. */
export function ToggleRow({
  checked,
  onChange,
  label,
  help,
  disabledReason,
  divider = true,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  help: string;
  disabledReason?: string | null;
  /** False when the row below belongs to the same setting as this one. */
  divider?: boolean;
}) {
  const disabled = Boolean(disabledReason);
  return (
    <div className={cn("w-full py-3", divider && "border-b border-line last:border-b-0")}>
      <div className="flex w-full items-center gap-3">
        <BoxCheckbox
          checked={checked}
          onChange={onChange}
          disabled={disabled}
          ariaLabel={label}
        />
        <button
          type="button"
          disabled={disabled}
          className={cn(
            "min-w-0 flex-1 text-left text-[14px] font-semibold leading-5",
            disabled ? "cursor-not-allowed text-muted" : "text-ink",
          )}
          onClick={() => onChange(!checked)}
        >
          {label}
        </button>
        <HelpDot label={help} className="flex-none" />
      </div>
      {disabledReason && (
        <p className="mt-1.5 pl-8 text-[13px] leading-5 text-muted">{disabledReason}</p>
      )}
    </div>
  );
}
