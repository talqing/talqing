"use client";

import type { WhatsAppBatchResponse } from "@talqing/sdk";
import { Input, Segment } from "@/app/components/ui";
import { TermsRow, clamp } from "@/app/components/Pacing";

/* What happens to a message Meta holds back under the person's marketing limit:
 * it can go through days later, so the batch can send it again. `attempts`
 * counts the first send, as the API does. */

export type ResendDraft = { attempts: number; hours: number };

export const DEFAULT_RESEND: ResendDraft = { attempts: 3, hours: 48 };

export const resendFrom = (
  batch: Pick<WhatsAppBatchResponse, "delivery_attempts" | "delivery_retry_after_hours">,
): ResendDraft => ({ attempts: batch.delivery_attempts, hours: batch.delivery_retry_after_hours });

export const resendBody = (d: ResendDraft) => ({
  delivery_attempts: d.attempts,
  delivery_retry_after_hours: d.hours,
});

export function ResendRow({
  value,
  onChange,
}: {
  value: ResendDraft;
  onChange: (next: ResendDraft) => void;
}) {
  const on = value.attempts > 1;
  return (
    <TermsRow label="If held back">
      <Segment
        value={on ? "resend" : "stop"}
        onChange={(v) =>
          onChange({ ...value, attempts: v === "resend" ? DEFAULT_RESEND.attempts : 1 })
        }
        options={[
          { value: "stop", label: "Give up" },
          { value: "resend", label: "Send again" },
        ]}
      />
      {on && (
        <div className="flex basis-full flex-wrap items-center gap-2 text-[13.5px] text-ink-soft">
          <span>Up to</span>
          <Input
            type="number"
            min={1}
            max={4}
            value={value.attempts - 1}
            onChange={(e) => onChange({ ...value, attempts: clamp(e.target.value, 1, 4) + 1 })}
            aria-label="Times to send again"
            className="w-[64px] text-right tabular-nums"
          />
          <span>more {value.attempts === 2 ? "time" : "times"},</span>
          <Input
            type="number"
            min={24}
            max={168}
            value={value.hours}
            onChange={(e) => onChange({ ...value, hours: clamp(e.target.value, 24, 168) })}
            aria-label="Hours between sends"
            className="w-[72px] text-right tabular-nums"
          />
          <span>hours apart</span>
        </div>
      )}
      <p className="basis-full text-[12.5px] leading-5 text-faint">
        When Meta holds a message back under the person&rsquo;s marketing limit.
      </p>
    </TermsRow>
  );
}
