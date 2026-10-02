"use client";
import React from "react";

/* The CoPilot answers in Markdown — it is an LLM, and nothing we can put in a
   prompt reliably stops one writing `- ` lists and `code` spans. Rendered as
   plain text those marks are litter: "- Language: Auto" and a field name in
   stray backticks, in the one place the product explains what it just changed
   to your agent.

   So this renders the subset it actually emits — paragraphs, dash lists, bold,
   inline code — and nothing else. No dependency, because the alternative is a
   full CommonMark pipeline for four constructs, and no raw HTML, because the
   text on the other side of this is model output.

   Anything unrecognised falls through as its literal characters, which is the
   honest failure: a heading the model decides to write shows up as "## Voice"
   rather than vanishing. If that starts happening, add it here. */

const BOLD_OR_CODE = /(\*\*[^*]+\*\*|`[^`]+`)/g;

/** `**bold**` and `` `code` `` inside one line. */
function inline(text: string, keyPrefix: string): React.ReactNode[] {
  return text.split(BOLD_OR_CODE).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key} className="font-semibold text-ink">{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return (
        <code key={key} className="rounded bg-subtle px-1 py-px font-mono text-[0.9em] text-ink-soft">
          {part.slice(1, -1)}
        </code>
      );
    }
    return <React.Fragment key={key}>{part}</React.Fragment>;
  });
}

const BULLET = /^\s*[-*]\s+/;

/** Consecutive lines of the same kind, in order: one <ul> or one <p> each. */
function runs(lines: string[]): { bullets: boolean; lines: string[] }[] {
  const out: { bullets: boolean; lines: string[] }[] = [];
  for (const line of lines) {
    const bullets = BULLET.test(line);
    const last = out[out.length - 1];
    if (last && last.bullets === bullets) last.lines.push(line);
    else out.push({ bullets, lines: [line] });
  }
  return out;
}

export function CopilotProse({ text }: { text: string }) {
  // Split on blank lines, then again wherever a block changes between prose and
  // list. Both passes are needed: the model writes "She now:" immediately above
  // its bullets as often as it leaves a blank line there, and treating that as
  // one paragraph is what renders the dashes as literal text.
  const groups = text
    .trim()
    .split(/\n{2,}/)
    .flatMap((block) => runs(block.split("\n")));

  return (
    /* This is the answer you came for, so it is set like body copy and not like
       a caption: near-black, 14px, and open enough to read a paragraph in. */
    <div className="flex flex-col gap-2.5 text-[14px] leading-[1.62] text-ink">
      {groups.map((group, g) =>
        group.bullets ? (
          <ul key={g} className="flex flex-col gap-1 pl-1">
            {group.lines.map((line, i) => (
              <li key={i} className="flex gap-2">
                {/* A dot rather than a list-marker: at this size the browser's
                    own bullet sits too low and too far out to line up with the
                    first line of wrapped text. */}
                <span aria-hidden className="mt-[7px] h-1 w-1 flex-none rounded-full bg-line-strong" />
                <span className="min-w-0">{inline(line.replace(BULLET, ""), `${g}-${i}`)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p key={g} className="whitespace-pre-wrap">
            {inline(group.lines.join("\n"), String(g))}
          </p>
        ),
      )}
    </div>
  );
}
