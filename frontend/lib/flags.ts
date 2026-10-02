// Country-flag helpers for the voice pickers. A voice's `country` is an ISO
// alpha-2 code (→ a real flag); a `language` is a code (en, hi, …) and an
// `accent` is a free-form name (American, Peninsular, Marathi, …) — neither is a
// country, so each maps to a REPRESENTATIVE country for the flag. Unknown inputs
// return "" so callers render no flag (never a broken glyph). NOTE: on Windows,
// flag emoji fall back to the two-letter code rather than a flag — that's a
// platform limitation, still legible.

// ISO 3166-1 alpha-2 → regional-indicator flag emoji. "" for non two-letter codes.
export function flagEmoji(country?: string | null): string {
  if (!country) return "";
  const cc = country.trim().toUpperCase();
  if (!/^[A-Z]{2}$/.test(cc)) return "";
  const A = 0x1f1e6; // regional indicator "A"
  return String.fromCodePoint(A + (cc.charCodeAt(0) - 65), A + (cc.charCodeAt(1) - 65));
}

// language code → representative country (the language's home/most-recognized
// flag, NOT a specific accent). India-first for our regional languages.
// Only BARE codes belong here — anything carrying a region (en-US, pt-BR) gets
// its flag from the region subtag instead, see `langFlag`.
const LANG_COUNTRY: Record<string, string> = {
  en: "GB", es: "ES", fr: "FR", de: "DE", it: "IT", pt: "PT", nl: "NL",
  pl: "PL", ru: "RU", uk: "UA", tr: "TR", ar: "SA", ja: "JP", ko: "KR",
  zh: "CN", th: "TH", vi: "VN", id: "ID", da: "DK", fi: "FI",
  no: "NO", sv: "SE",
  // India-first regional languages
  hi: "IN", ta: "IN", te: "IN", bn: "IN", mr: "IN", kn: "IN", ml: "IN",
  gu: "IN", pa: "IN", ur: "PK",
  // the rest of the agent language picker (services/catalog/languages.py)
  af: "ZA", hy: "AM", az: "AZ", be: "BY", bs: "BA", bg: "BG", hr: "HR",
  cs: "CZ", et: "EE", el: "GR", he: "IL", hu: "HU", is: "IS", kk: "KZ",
  lv: "LV", lt: "LT", mk: "MK", mi: "NZ", ne: "NP", fa: "IR", ro: "RO",
  sr: "RS", sk: "SK", sl: "SI", sw: "TZ", ms: "MY", cy: "GB",
  fil: "PH", tl: "PH",
  // ca/gl are regional languages of Spain and taq (Tamasheq) is trans-Saharan;
  // none has a country whose flag would be more honest than none at all.
};

// The region subtag of a language tag, when it is one. Only a two-LETTER subtag
// is a country: `hi-Latn` names a script and `es-419` a UN M49 area, and a flag
// for either would be a guess (Spanish for Latin America is not Spain's).
function regionSubtag(code: string): string {
  const parts = code.split("-");
  const last = parts[parts.length - 1];
  return parts.length > 1 && /^[A-Za-z]{2}$/.test(last) ? last : "";
}

// accent name (case-insensitive) → representative country.
const ACCENT_COUNTRY: Record<string, string> = {
  american: "US", british: "GB", english: "GB", scottish: "GB", welsh: "GB",
  irish: "IE", australian: "AU", "new zealand": "NZ", canadian: "CA",
  // Spanish accents
  peninsular: "ES", castilian: "ES", argentine: "AR", colombian: "CO",
  mexican: "MX",
  // other provider accent labels
  dutch: "NL", filipino: "PH", french: "FR", german: "DE", italian: "IT",
  japanese: "JP", portuguese: "PT", brazilian: "BR",
  // India-first regional accents
  indian: "IN", hindi: "IN", marathi: "IN", punjabi: "IN", gujarati: "IN",
  bengali: "IN", bhojpuri: "IN", awadhi: "IN", bihari: "IN", haryanvi: "IN",
  rajasthani: "IN", tamil: "IN", telugu: "IN", telegu: "IN", kannada: "IN",
  malayalam: "IN",
  // "standard" / "latin american" have no single country → no flag
};

// flag for a language code (e.g. "en" → 🇬🇧, "en-US" → 🇺🇸). "" when unmapped.
// A named region wins over the language's representative country, and there is
// deliberately no fall back from an unmappable subtag to the bare language:
// "Spanish (Latin America)" under a Spanish flag is worse than no flag.
export function langFlag(code?: string | null): string {
  if (!code) return "";
  const trimmed = code.trim();
  const mapped = LANG_COUNTRY[trimmed.toLowerCase()] || LANG_COUNTRY[trimmed];
  return flagEmoji(mapped || regionSubtag(trimmed));
}

// flag for an accent name (e.g. "American" → 🇺🇸). "" when unmapped.
export function accentFlag(accent?: string | null): string {
  if (!accent) return "";
  return flagEmoji(ACCENT_COUNTRY[accent.trim().toLowerCase()] || "");
}

// "🇺🇸 American" / "American" — prefix a label with its flag when one exists.
export function withFlag(flag: string, label: string): string {
  return flag ? `${flag} ${label}` : label;
}
