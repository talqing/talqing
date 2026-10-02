"use client";

import { Modal, Tooltip } from "@/app/components/ui";
import { OperationIcon } from "./OperationFlowchart";
import { KIND_DESCRIPTION, KIND_LABEL, OPERATION_GROUPS, TERMINAL_KINDS, type OperationKind } from "./operationMetadata";

const ENDS_THE_CHAIN = "Ends the chain — nothing can follow it.";

/* The card shows the name; this is everything else about the operation, on
   hover and on focus. */
function detail(kind: OperationKind): JSX.Element {
  return (
    <>
      {KIND_DESCRIPTION[kind]}
      {TERMINAL_KINDS.includes(kind) && (
        <span className="mt-1 block text-surface/70">{ENDS_THE_CHAIN}</span>
      )}
    </>
  );
}

function detailText(kind: OperationKind): string {
  const terminal = TERMINAL_KINDS.includes(kind) ? ` ${ENDS_THE_CHAIN}` : "";
  return `${KIND_DESCRIPTION[kind]}${terminal}`;
}

export function OperationPicker({
  onPick,
  onClose,
}: {
  onPick: (kind: OperationKind) => void;
  onClose: () => void;
}): JSX.Element {
  return (
    <Modal
      title="Add an operation"
      sub="Operations run top to bottom, in the order you add them."
      onClose={onClose}
      /* Three roomy columns. The cards carry a name and nothing else, so the
         width buys tile size rather than more text. */
      width="max-w-[880px]"
    >
      <div className="mt-1 flex flex-col gap-5 pb-6">
        {OPERATION_GROUPS.map((group) => (
          <section key={group.title} className="flex flex-col gap-2.5">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">{group.title}</h3>
            {/* Three across is what puts all eleven on screen at once, which is
                the whole point — every group but one is exactly three wide. */}
            <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
              {group.kinds.map((kind) => (
                /* focusable={false} because the trigger already is one: Tooltip's
                   wrapper would otherwise add a second tab stop in front of every
                   card. Focus on the button still bubbles up and opens the
                   bubble, so this is not mouse-only — and the same words are in
                   the button's accessible name below, since dropping the
                   wrapper's tabIndex also drops its aria-describedby. */
                <Tooltip key={kind} label={detail(kind)} focusable={false} className="w-full">
                  <button
                    type="button"
                    onClick={() => onPick(kind)}
                    /* Floating cards: they rest on a hairline shadow and lift
                       2px under the cursor, then press back down on click. The
                       lift is the only motion in the dialog, so it reads as
                       "this is the thing you are about to pick" rather than as
                       decoration. */
                    className="group flex w-full cursor-pointer items-center gap-3.5 rounded-xl border border-line-2 bg-white px-4 py-4 text-left shadow-rest transition-[transform,box-shadow,border-color] duration-150 ease-out hover:-translate-y-0.5 hover:border-line-strong hover:shadow-pop focus:outline-none focus-visible:border-ink focus-visible:ring-2 focus-visible:ring-ink/10 active:translate-y-0 active:shadow-rest motion-reduce:transition-none motion-reduce:hover:transform-none"
                  >
                    <span className="grid h-10 w-10 flex-none place-items-center rounded-lg bg-subtle text-ink-soft transition-colors group-hover:bg-ink group-hover:text-white">
                      <OperationIcon kind={kind} className="h-5 w-5" />
                    </span>
                    <span className="min-w-0 flex-1 truncate text-[14px] font-semibold leading-5 text-ink">
                      {KIND_LABEL[kind]}
                    </span>
                    <span className="sr-only">{detailText(kind)}</span>
                  </button>
                </Tooltip>
              ))}
            </div>
          </section>
        ))}
      </div>
    </Modal>
  );
}
