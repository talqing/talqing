import type { WhatsAppTemplate } from "@talqing/sdk";
import type { ListCopy } from "@/app/email/outbound/RecipientEditor";

/** The list's copy for this template: its columns, and a sample file with them.
 *  A new batch starts from `phone` plus one column per variable; an existing one
 *  passes its own columns and phone column. */
export function listCopy(
  template: WhatsAppTemplate | undefined,
  own?: { columns: string[]; toColumn: string },
): ListCopy {
  const variables = template?.variables ?? [];
  const toColumn = own?.toColumn ?? "phone";
  const columns = own?.columns ?? [toColumn, ...variables];
  const example = (c: string) =>
    c === toColumn ? "+919876543210" : /name/i.test(c) ? "Asha" : `your ${c.replace(/_/g, " ")}`;
  return {
    starterColumns: columns,
    sample: {
      csv: `${columns.join(",")}\n${columns.map(example).join(",")}\n`,
      fileName: `${template?.name ?? "whatsapp"}-sample.csv`,
    },
    composerNote: (
      <>
        Numbers with their country code, like +91 98765 43210. Every column also reaches the
        agent as <code className="font-mono text-[12px] text-ink-soft">{"{{userdata.name}}"}</code>{" "}
        when they reply.
      </>
    ),
    uploadNote: (
      <>
        each one can fill a blank in the template and reaches the agent as{" "}
        <code className="font-mono text-[12px] text-ink-soft">{"{{userdata.name}}"}</code>.
      </>
    ),
    emptyNote:
      "Nothing on the list yet. Every row you add shows up below, exactly as it will be sent, before anything goes out.",
  };
}
