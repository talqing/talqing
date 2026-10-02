import type { FaqEntryInput } from "@talqing/sdk";
import { parseCsv } from "@/app/telephony/outbound-calling/recipients";

/** A `question,answer` CSV — Dialogflow's FAQ format — as entries to append.
 *
 *  Only the file's SHAPE is judged here: the header, and that every row has
 *  both cells. Length limits, duplicates and the 500 cap are the API's to
 *  refuse, so the browser and the server cannot disagree about a rule. */
export function parseFaqCsv(text: string): { entries: FaqEntryInput[] } | { problem: string } {
  const [header, ...rows] = parseCsv(text);
  const columns = (header ?? []).map((cell) => cell.trim().toLowerCase());
  if (columns.length !== 2 || columns[0] !== "question" || columns[1] !== "answer") {
    return { problem: "The first row must be exactly: question,answer" };
  }
  if (rows.length === 0) return { problem: "This file has a header and no questions." };

  const entries: FaqEntryInput[] = [];
  for (const [index, cells] of rows.entries()) {
    // Counted the way a spreadsheet shows it, header included.
    const row = index + 2;
    if (cells.slice(2).some((cell) => cell.trim() !== "")) {
      return { problem: `Row ${row} has more than two columns. Quote any text that contains a comma.` };
    }
    const question = (cells[0] ?? "").trim();
    const answer = (cells[1] ?? "").trim();
    if (!question) return { problem: `Row ${row} has no question.` };
    if (!answer) return { problem: `Row ${row} has no answer.` };
    entries.push({ question, answer });
  }
  return { entries };
}
