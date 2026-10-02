"use client";

import { cn } from "@/lib/cn";
import type { TelephonyAccountResponse, TelephonyProviderSpec } from "@talqing/sdk";
import { CarrierLogo } from "./CarrierLogo";

/**
 * The carriers a workspace can connect, as a grid of choices.
 *
 * Shared by the import wizard's first step and the page's empty state, because
 * they ask the same question. A workspace with no carriers should not be shown
 * a paragraph about carriers and a button that reveals this grid — the grid is
 * the answer, so it goes on the page.
 */
export function CarrierPicker({
  catalog,
  accounts,
  onPick,
  className,
}: {
  catalog: TelephonyProviderSpec[];
  accounts: TelephonyAccountResponse[];
  onPick: (spec: TelephonyProviderSpec) => void;
  className?: string;
}) {
  return (
    <div className={cn("grid grid-cols-2 gap-3 sm:grid-cols-4", className)}>
      {catalog.map((spec) => {
        const connected = accounts.filter(
          (a) => a.provider === spec.provider && a.status !== "disabled",
        ).length;
        return (
          <button
            key={spec.provider}
            type="button"
            disabled={!spec.implemented}
            onClick={() => onPick(spec)}
            className={cn(
              "group flex flex-col items-center gap-2.5 rounded-xl border px-4 py-6 text-center transition-colors",
              spec.implemented
                ? "border-line-2 bg-white hover:border-line-strong hover:bg-canvas focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/10"
                : "cursor-not-allowed border-line bg-subtle opacity-60",
            )}
          >
            <span className="grid h-11 w-11 place-items-center rounded-xl border border-line-2 bg-white text-ink">
              <CarrierLogo spec={spec} size={24} />
            </span>
            <span className="text-[14.5px] font-semibold leading-5 text-ink">{spec.label}</span>
            <span className="text-[12px] font-medium leading-4 text-faint">
              {!spec.implemented
                ? "Coming soon"
                : connected === 0
                  ? "Not connected"
                  : `${connected} account${connected === 1 ? "" : "s"}`}
            </span>
          </button>
        );
      })}
    </div>
  );
}
