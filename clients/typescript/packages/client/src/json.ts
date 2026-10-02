/* Free-form JSON: userdata, a frontend-RPC payload.
 *
 * `@talqing/sdk` declares the same three aliases for the same reason, and this
 * package deliberately does not import them from there: the whole point of the
 * split is that the call protocol needs no API client. These are structural
 * aliases, so the two spellings are the same type to a consumer holding both.
 *
 * `JsonObject` is exactly what the API says such a field is — an object of
 * `unknown` — so a value read off a response drops straight into it. Naming it
 * anything narrower would assert a guarantee the API does not make.
 *
 * `JsonValue` is the recursive form, for a payload the CALLER builds: there it
 * is a real constraint, because it refuses a `Date` or a class instance that
 * would not survive `JSON.stringify`. */
export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: unknown };
