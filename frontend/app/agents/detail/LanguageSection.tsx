"use client";
import type { LanguageOption } from "@talqing/sdk";
import { Label, Select } from "@/app/components/ui";
import { langFlag } from "@/lib/flags";
import { AUTO_LANGUAGE } from "./agentConfig";

/* Just the picker. There is deliberately no per-model summary here and no
   sentence explaining Auto: every speech model we carry either detects the
   language or is given one, and any model that cannot will be defaulted to
   English rather than explained away in the editor. A caveat under a dropdown
   is a design we did not get right, dressed up as documentation. */
export function LanguageSection({
  languages,
  value,
  onChange,
  note,
}: {
  languages: LanguageOption[];
  value: string | null | undefined;
  onChange: (language: string) => void;
  /** A qualification of what this setting reaches, said beside it. */
  note?: string | null;
}) {
  const named = languages.find((l) => l.code.toLowerCase() === (value || "").toLowerCase());

  return (
    <div className="flex flex-col gap-2">
      <Label>Language</Label>
      <div className="md:max-w-[260px]">
        <Select searchable value={named?.code ?? AUTO_LANGUAGE} onChange={(e) => onChange(e.target.value)}>
          <option value={AUTO_LANGUAGE}>Auto</option>
          {languages.map((lang) => (
            <option key={lang.code} value={lang.code}>
              {`${langFlag(lang.code) || ""} ${lang.name}`.trim()}
            </option>
          ))}
        </Select>
      </div>
      {note && <p className="text-[13px] leading-5 text-muted">{note}</p>}
    </div>
  );
}
