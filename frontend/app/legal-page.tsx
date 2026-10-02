// Shell and prose primitives for /privacy and /terms.
//
// The two documents are long and structurally identical, so the markup lives
// here and the pages stay pure content — which is the point: these are files a
// non-engineer will have to read, amend after legal review, and keep in sync
// with what the platform actually does.
//
// A page hands over a `sections` array rather than nesting <Section> children,
// so the table of contents and the body are rendered from ONE list. Section
// numbers come from that list's order too: renumbering by hand after an insert
// is exactly the kind of drift a legal document cannot afford, and the numbers
// are referenced across the two files ("section 8 of the privacy policy").
import type { ReactNode } from "react";

import { Footer, MarketingShell, SiteHeader } from "./marketing-chrome";

export type LegalSection = {
  /** The anchor. Stable across edits — these are linked from elsewhere. */
  id: string;
  heading: string;
  body: ReactNode;
};

export function LegalPage({
  title,
  summary,
  updated,
  sections,
}: {
  title: string;
  summary: string;
  updated: string;
  sections: LegalSection[];
}) {
  return (
    <MarketingShell>
      <SiteHeader />
      {/* Wide enough for a 70ch reading measure plus the contents rail, and no
          wider — this is long-form prose, not the landing page's 1400px
          container. The header is fixed, so pt- clears it. */}
      <div className="relative z-10 mx-auto max-w-[calc(70ch+15rem+4rem)] px-6 pb-24 pt-36 lg:pt-44">
        <div className="xl:grid xl:grid-cols-[minmax(0,70ch)_15rem] xl:gap-16">
          {/* The grid only exists at xl, where the column is already capped at
              the reading measure. Below it there is no grid, so the measure has
              to be held here or the prose runs the full container width. */}
          <article className="mx-auto max-w-[70ch] xl:mx-0 xl:max-w-none">
            <header className="border-b border-foreground/10 pb-12">
              <p className="mb-5 inline-flex items-center gap-3 font-mono text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">
                <span className="h-px w-9 bg-[#5b7d58]" />
                Legal
              </p>
              <h1 className="font-display text-5xl leading-[1.04] lg:text-6xl">
                {title}
              </h1>
              <p className="mt-6 text-lg leading-relaxed text-muted-foreground">
                {summary}
              </p>
              <p className="mt-8 font-mono text-xs uppercase tracking-[0.14em] text-muted-foreground">
                Last updated {updated}
              </p>
            </header>
            {sections.map((section, i) => (
              <section
                key={section.id}
                id={section.id}
                className="scroll-mt-28 border-b border-foreground/10 py-12"
              >
                <h2 className="mb-6 font-display text-2xl leading-snug lg:text-3xl">
                  <span className="mr-3 text-muted-foreground">{i + 1}.</span>
                  {section.heading}
                </h2>
                <div className="space-y-5 leading-relaxed text-muted-foreground">
                  {section.body}
                </div>
              </section>
            ))}
          </article>

          {/* Desktop only, deliberately. On a phone these documents are read by
              scrolling, and a fourteen-item list wedged between the title and
              the first sentence costs more than it gives. */}
          <nav
            aria-label="Contents"
            className="hidden xl:sticky xl:top-32 xl:block xl:h-fit"
          >
            <p className="font-mono text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">
              Contents
            </p>
            <ol className="mt-5 space-y-2.5">
              {sections.map((section, i) => (
                <li key={section.id} className="flex gap-2.5 text-[13.5px] leading-snug">
                  <span className="w-4 shrink-0 text-right font-mono text-[12px] text-muted-foreground/60">
                    {i + 1}
                  </span>
                  <a
                    href={`#${section.id}`}
                    className="text-muted-foreground transition-colors hover:text-foreground"
                  >
                    {section.heading}
                  </a>
                </li>
              ))}
            </ol>
          </nav>
        </div>
      </div>
      <Footer />
    </MarketingShell>
  );
}

// A heading inside a section. Only section 4 of the privacy policy needs one —
// its two halves are a real structural split, not emphasis — so this stays a
// plain h3 rather than growing a numbering scheme nobody references.
export function Subheading({ children }: { children: ReactNode }) {
  return (
    <h3 className="!mt-9 text-[17px] font-semibold text-foreground">{children}</h3>
  );
}

export function Bullets({ items }: { items: ReactNode[] }) {
  return (
    <ul className="space-y-3">
      {items.map((item, i) => (
        <li key={i} className="flex gap-3">
          <span
            aria-hidden
            className="mt-[0.6em] h-1 w-1 shrink-0 rounded-full bg-[#5b7d58]"
          />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

// A two-column reference table — sub-processors, retention periods. Scrolls
// inside its own container so a narrow viewport never scrolls the page body.
export function DataTable({
  columns,
  rows,
}: {
  columns: [string, string];
  rows: [ReactNode, ReactNode][];
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-foreground/15">
            {columns.map((c) => (
              // Headers are two or three words; letting them wrap squeezes the
              // first column to nothing when the second holds a paragraph.
              <th
                key={c}
                className="whitespace-nowrap py-3 pr-6 font-medium text-foreground"
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([a, b], i) => (
            <tr key={i} className="border-b border-foreground/10 align-top">
              <td className="py-3 pr-6 font-medium text-foreground">{a}</td>
              <td className="py-3 pr-6">{b}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// For the handful of paragraphs a reader must not skim past — the ones that
// qualify a claim made a line earlier, or that hand an obligation to the
// customer. Used sparingly: four of these on a page and none of them lands.
export function Note({
  heading,
  children,
}: {
  heading: string;
  children: ReactNode;
}) {
  return (
    <div className="border-l-2 border-[#5b7d58] bg-secondary/60 px-6 py-5">
      <p className="mb-2.5 text-sm font-semibold text-foreground">{heading}</p>
      <div className="space-y-4 text-[15px] leading-relaxed">{children}</div>
    </div>
  );
}
