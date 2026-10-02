"use client";

import { useState } from "react";
import { Button, CopyButton, btn } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import type { TelephonyAccountResponse } from "@talqing/sdk";

/** Arrow marking a link that leaves Talqing for the carrier's own site. */
function ExternalIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden className="flex-none opacity-70">
      <path d="M7 17 17 7M9 7h8v8" />
    </svg>
  );
}

/**
 * The carrier-console work Talqing cannot do over an API.
 *
 * Rendered from `account.setup_steps`, which the backend builds — this
 * component knows nothing about Exotel, App Bazaar or trunk SIDs. A carrier
 * that needs no console work simply has no steps and renders nothing.
 *
 * It renders as a band inside its carrier's group rather than as a card
 * floating above the table: it is the reason every number underneath says
 * "Setup needed", and the two should be read in that order.
 *
 * Instructions are numbered and carry their own deep link and paste value,
 * because the alternative — one paragraph of prose — is what sent a tenant
 * hunting through the carrier's console for a setting that does not exist
 * there. Anything a tenant has to look up elsewhere is a defect in this band.
 */
export function CarrierSetupCard({
  account,
  onConfirmed,
  onError,
  className,
}: {
  account: TelephonyAccountResponse;
  onConfirmed: (message: string) => void;
  onError: (message: string) => void;
  className?: string;
}) {
  const [saving, setSaving] = useState(false);
  const pending = account.setup_steps.filter((step) => !step.done);
  if (pending.length === 0) return null;
  const confirmable = pending.some((step) => step.user_confirmable);

  async function confirm() {
    setSaving(true);
    try {
      await api.patchTelephonyAccount(account.id, { console_setup_confirmed: true });
      onConfirmed(`${account.display_name}: carrier setup confirmed. Inbound is now open.`);
    } catch (error: unknown) {
      onError(apiErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={cn("border-b border-warn/20 bg-warn/[0.05] px-4 py-3.5", className)}>
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-[13px] font-semibold leading-5 text-warn">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden className="flex-none">
            <path d="M12 3 2 20h20L12 3Z" />
            <path d="M12 9v5M12 17h.01" />
          </svg>
          Incoming calls are blocked until you finish setup at the carrier
        </div>

        <div className="mt-3 grid gap-5">
          {pending.map((step) => (
            <div key={step.key} className="min-w-0">
              <div className="text-[13px] font-medium leading-5 text-ink">{step.title}</div>
              {step.detail && (
                <p className="mt-1 max-w-[76ch] text-[12.5px] leading-[18px] text-ink-soft">
                  {step.detail}
                </p>
              )}

              {/* Capped, not full-bleed: each row puts its "Open …" link at the
                  far end, so on a wide screen an uncapped list strands the button
                  a screen-width away from the sentence it belongs to. */}
              {step.instructions.length > 0 && (
                <ol className="mt-3 grid max-w-[820px] gap-2.5">
                  {step.instructions.map((instruction, index) => (
                    <li
                      key={index}
                      className="grid grid-cols-[20px_minmax(0,1fr)] items-start gap-x-2.5 gap-y-2"
                    >
                      {/* Numbers are rendered rather than list-style so the marker
                          keeps its column when a step wraps to several lines. */}
                      <span
                        aria-hidden
                        className="mt-px flex h-5 w-5 flex-none items-center justify-center rounded-full border border-line-2 bg-white text-[11px] font-semibold leading-none text-ink-soft tabular-nums"
                      >
                        {index + 1}
                      </span>
                      <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-4 gap-y-1.5">
                        <p className="min-w-0 max-w-[70ch] flex-1 text-[12.5px] leading-[18px] text-ink">
                          {instruction.text}
                        </p>
                        {instruction.url && (
                          <a
                            href={instruction.url}
                            target="_blank"
                            rel="noreferrer noopener"
                            className={cn(btn("secondary", "sm"), "flex-none gap-1.5 self-start")}
                          >
                            {instruction.url_label || "Open"}
                            <ExternalIcon />
                          </a>
                        )}
                      </div>
                      {instruction.copy_value && (
                        <div className="col-start-2 min-w-0">
                          <div className="flex items-center gap-2">
                            {/* Sized to the value, not to the row: a trunk SID is
                                ~32 characters and a field stretched past it reads
                                as an empty input waiting to be filled in. */}
                            <code className="min-w-0 max-w-[46ch] truncate rounded-lg border border-line-2 bg-white px-2.5 py-1.5 font-mono text-[12.5px] leading-5 text-ink">
                              {instruction.copy_value}
                            </code>
                            <CopyButton
                              value={instruction.copy_value}
                              label="Copy"
                              ariaLabel="Copy this value"
                              className="flex-none py-1.5"
                            />
                          </div>
                          {instruction.copy_hint && (
                            <p className="mt-1.5 max-w-[70ch] text-[12px] leading-[17px] text-muted">
                              {instruction.copy_hint}
                            </p>
                          )}
                        </div>
                      )}
                    </li>
                  ))}
                </ol>
              )}

              {step.doc_url && (
                <a
                  href={step.doc_url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="mt-3 inline-flex items-center gap-1.5 text-[12.5px] leading-5 text-ink-soft underline decoration-line-strong underline-offset-2 hover:text-ink"
                >
                  {step.doc_label || "Carrier documentation"}
                  <ExternalIcon />
                </a>
              )}
            </div>
          ))}
        </div>

        {/* Last in the band, because confirming is the last step: no API can
            check this work, so the tenant saying so is the only signal there
            is, and it should be reachable only after reading what to do. */}
        {confirmable && (
          <Button size="sm" disabled={saving} onClick={() => void confirm()} className="mt-4">
            {saving ? "Confirming…" : "I've done this"}
          </Button>
        )}
      </div>
    </div>
  );
}
