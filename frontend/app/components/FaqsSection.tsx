"use client";

import Link from "next/link";
import type { FaqSelection, FaqSummary } from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { Badge, BoxCheckbox, CardHead, LIFT_ON_HOVER, NothingToAttach, Panel } from "./ui";

/** Matches `AgentBase.faqs`'s `max_length` on the API. */
const MAX_ATTACHED = 10;

/** A speech bubble holding a question mark — the nav's FAQ glyph, sized by the
 *  caller. */
export const FaqIcon = ({ className }: { className?: string }) => (
  <svg
    className={className}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
  >
    <path d="M4.5 18.5V7A2.5 2.5 0 0 1 7 4.5h10A2.5 2.5 0 0 1 19.5 7v7a2.5 2.5 0 0 1-2.5 2.5H9l-4.5 3Z" />
    <path d="M9.8 9.2a2.3 2.3 0 1 1 3.3 2.1c-.7.4-1.1.8-1.1 1.5M12 14.6v.1" />
  </svg>
);

export const questionCount = (n: number): string => `${n} question${n === 1 ? "" : "s"}`;

/** The FAQ picker both editors show: every FAQ in the workspace, ticked when
 *  this config attaches it. A stored config only ever holds `{faq_id}` — the
 *  API turns an inline FAQ into a stored one on write — so that is all it
 *  toggles. */
export function FaqsSection({
  subject,
  faqs,
  selected,
  onChange,
  className,
}: {
  subject: "agent" | "task";
  faqs: FaqSummary[];
  selected: FaqSelection[];
  onChange: (next: FaqSelection[]) => void;
  className?: string;
}) {
  const full = selected.length >= MAX_ATTACHED;

  function toggle(faqId: string) {
    onChange(
      selected.some((sel) => sel.faq_id === faqId)
        ? selected.filter((sel) => sel.faq_id !== faqId)
        : [...selected, { faq_id: faqId }],
    );
  }

  return (
    <Panel className={cn(LIFT_ON_HOVER, className)}>
      <CardHead title="FAQs" desc={`Questions this ${subject} answers with your written answers`}>
        {faqs.length > 0 && <Badge>{selected.length} attached</Badge>}
      </CardHead>
      {faqs.length === 0 ? (
        <NothingToAttach what="FAQs" href="/faqs" action="Create an FAQ">
          Write the questions people ask and the answers you want given, word for word.
        </NothingToAttach>
      ) : (
        <div className="flex flex-col gap-1">
          {faqs.map((faq) => {
            const attached = selected.some((sel) => sel.faq_id === faq.id);
            const disabled = full && !attached;
            return (
              <div
                key={faq.id}
                title={disabled ? `Up to ${MAX_ATTACHED} FAQs can be attached.` : undefined}
                className={cn(
                  "flex items-center gap-3 rounded-lg border border-transparent px-3 py-2.5 transition-colors hover:bg-subtle",
                  disabled && "opacity-50",
                )}
              >
                <BoxCheckbox
                  checked={attached}
                  disabled={disabled}
                  onChange={() => toggle(faq.id)}
                  ariaLabel={`Attach ${faq.name}`}
                />
                <span className="grid h-7 w-7 flex-none place-items-center rounded-lg border border-line-2 bg-white text-ink-soft">
                  <FaqIcon className="h-[15px] w-[15px]" />
                </span>
                <button
                  type="button"
                  onClick={() => !disabled && toggle(faq.id)}
                  className="flex min-w-0 flex-1 cursor-pointer items-baseline gap-2 text-left"
                >
                  <strong className="truncate text-[13.5px] font-semibold text-ink">{faq.name}</strong>
                  <span
                    className={cn(
                      "flex-none text-[12.5px]",
                      attached && faq.entry_count === 0 ? "text-warn" : "text-muted",
                    )}
                  >
                    {faq.entry_count === 0 ? "No questions yet" : questionCount(faq.entry_count)}
                  </span>
                </button>
                <Link
                  href={`/faqs/detail?id=${faq.id}`}
                  className="flex-none text-[12.5px] text-muted underline-offset-2 hover:text-ink hover:underline"
                >
                  Edit
                </Link>
              </div>
            );
          })}
        </div>
      )}
      <p className="mt-3 text-[12.5px] leading-5 text-muted">
        Edits to an FAQ go live without publishing.
      </p>
    </Panel>
  );
}
