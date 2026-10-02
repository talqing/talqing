import type { CatalogResponse } from "@talqing/sdk";

/* A publish and a run both refuse to proceed when the workspace has no key for
   a provider the agent needs — `backend/services/agents/validate.py` and
   `backend/services/tasks/run.py` write that sentence. It arrives here as one
   more string in `errors`, so the provider it blames has to be read back out of
   the prose before we can offer to fix it in place.

   The match is deliberately narrow: the opening clause, and then the label
   looked up in the catalog both sides name providers from. An error that does
   not resolve to a real provider — the copy changed, the provider left the
   catalog — renders as the plain text it always did, never as a button that
   opens the wrong key box. */
const MISSING_KEY = /^no (.+?) API key for this workspace\b/;

/** The provider a missing-BYOK-key error names, or null for any other error. */
export function missingKeyProvider(
  error: string,
  catalog: CatalogResponse,
): { id: string; label: string } | null {
  const label = MISSING_KEY.exec(error)?.[1];
  if (!label) return null;
  const found = Object.entries(catalog.providers).find(([, meta]) => meta.label === label);
  return found ? { id: found[0], label: found[1].label } : null;
}
