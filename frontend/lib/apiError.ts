import { TalqingApiError } from "@talqing/sdk";

/* One way to turn a thrown value into something a user can read.

   Before this, seventeen pages each rolled their own: `catch (e: any)` then
   `e.message`, a local `errMessage()`, an inline `instanceof Error ? …`, or a
   cast to a hand-written ApiError shape. They disagreed on what a non-Error
   throw should say and on whether a 401 was theirs to handle. The API answers
   with one error shape; the client should read it in one place. */

const SESSION_EXPIRED = "Your session expired. Redirecting to sign-in…";

/** Message to show the user for anything thrown by the SDK, fetch, or the runtime. */
export function apiErrorMessage(error: unknown, fallback = "Something went wrong"): string {
  if (error instanceof TalqingApiError) {
    // A 401 is already being handled globally (see lib/api.ts) — say what is
    // happening rather than leaking "Unauthorized" from the transport layer.
    return error.status === 401 ? SESSION_EXPIRED : error.message || fallback;
  }
  if (error instanceof Error) return error.message || fallback;
  return fallback;
}

/** The API's field-level validation errors, when it sent any. Empty otherwise. */
export function apiErrorList(error: unknown): string[] {
  if (!(error instanceof TalqingApiError)) return [];
  return error.errors.map((entry) =>
    typeof entry === "string" ? entry : JSON.stringify(entry),
  );
}
