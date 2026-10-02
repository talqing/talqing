/** The hand-written half of the SDK: everything the OpenAPI document cannot say.
 *
 * The API surface itself is generated into `./gen` from `openapi/openapi.json`
 * and is never edited here. What is left is the client object that carries
 * credentials, the error type, the two browser-redirect URLs that are
 * deliberately absent from the document, and a pagination helper.
 */

import { TalqingApi } from "./gen/index.js";
import { createClient, createConfig } from "./gen/client/index.js";
import type { Client } from "./gen/client/index.js";
import type { ErrorResponse } from "./gen/types.gen.js";

/* Free-form JSON: userdata, tool arguments, `vars`, a tool's JSON Schema.
 *
 * `JsonObject` is exactly what the document says such a field is — an object of
 * `unknown` — so a value read off a response drops straight into it. Naming it
 * anything narrower would be the SDK asserting a guarantee the API does not
 * make, and every read would then need a cast to get back to the truth.
 *
 * `JsonValue` is the recursive form, for a payload the CALLER builds: there it
 * is a real constraint, because it refuses a `Date` or a class instance that
 * would not survive `JSON.stringify`. */
export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: unknown };

type Awaitable<T> = T | Promise<T>;
type TokenProvider = string | (() => Awaitable<string | null | undefined>);

/** A page of results. The document spells each instantiation out by hand
 *  (`PageAgentResponse`) because OpenAPI has no generics; TypeScript is
 *  structural, so those satisfy this and generic helpers still work. */
export interface Page<T> {
  items: T[];
  has_more: boolean;
  limit: number;
  offset: number;
}

/**
 * Every generated method, with the `| undefined` that `throwOnError` makes
 * impossible removed.
 *
 * The generator emits `<ThrowOnError extends boolean = false>` on every method,
 * so a client configured to throw still returns `T | undefined` at the type
 * level and every call site would need a `!`. There is no setting for it. This
 * re-types the whole tree once instead.
 *
 * `Exclude<R, undefined>` rather than `NonNullable<R>`: the latter turns the
 * `void` of a 204 into `{}`. `...args: infer A` rather than named parameters:
 * it keeps each method's optional-argument arity, so `agents.list()` with no
 * argument still compiles.
 */
type Throwing<T> = {
  [K in keyof T]: T[K] extends (...args: infer A) => Promise<infer R>
    ? (...args: A) => Promise<Exclude<R, undefined>>
    : Throwing<T[K]>;
};

/* This package publishes an ESM build and a CommonJS build, and an application
 * whose dependency graph reaches both gets two distinct copies of the class
 * below. `instanceof` compares class identity, so an error thrown by one copy
 * would fall straight through `catch (e) { if (e instanceof TalqingApiError) }`
 * against the other — the exact pattern the README tells callers to write, and a
 * silent one, because the error just keeps propagating.
 *
 * A key from the global symbol registry is the one thing both copies agree on.
 * `Symbol.hasInstance` below makes `instanceof` ask for the brand instead of the
 * prototype, so the check holds no matter which copy raised it. */
const TALQING_API_ERROR: unique symbol = Symbol.for("talqing.TalqingApiError");

/** Every error this API returns, in the one shape it returns them in. */
export class TalqingApiError extends Error {
  readonly name = "TalqingApiError";
  readonly status: number;
  /** Per-field problems, when the failure had more than one. Always present. */
  readonly errors: string[];
  readonly response: Response;
  readonly [TALQING_API_ERROR] = true;

  static [Symbol.hasInstance](value: unknown): value is TalqingApiError {
    return typeof value === "object" && value !== null && TALQING_API_ERROR in value;
  }

  constructor(body: ErrorResponse, response: Response) {
    super(body.detail.message);
    this.status = response.status;
    this.errors = body.detail.errors;
    this.response = response;
  }
}

export interface TalqingClientOptions {
  /** Base URL of the Talqing API, e.g. `https://api.example.com`. Required,
   *  with no default: a bundler inlines this at build time, so a localhost
   *  fallback would silently ship a production bundle calling nothing. */
  baseUrl: string;
  /** A personal access token, or a function returning one. Server-side use. */
  token?: TokenProvider;
  /** `"include"` for the dashboard's cookie session. */
  credentials?: RequestCredentials;
  headers?: Record<string, string>;
  fetch?: typeof fetch;
  /**
   * Called when the API answers 401. The credentials are gone, so every other
   * call will fail identically — this is a session-level fact, not a per-call
   * one, and handling it here keeps every caller from re-deriving it. The error
   * is still thrown afterwards.
   */
  onUnauthorized?: (error: TalqingApiError) => void;
}

interface TalqingClientExtras {
  readonly baseUrl: string;
  /** The underlying transport, for interceptors and one-off requests. */
  readonly http: Client;
  /** Where to send the browser to authorize an integration. */
  oauthStartUrl(provider: string, integrationId?: string): string;
}

export interface TalqingClient extends Throwing<TalqingApi>, TalqingClientExtras {}

class TalqingClientImpl extends TalqingApi implements TalqingClientExtras {
  readonly baseUrl: string;
  readonly http: Client;

  constructor(options: TalqingClientOptions) {
    // Types do not reach a JavaScript caller, or a value that is `string` but empty.
    if (!options?.baseUrl?.trim()) {
      throw new Error("TalqingClient requires a baseUrl (e.g. https://api.in.talqing.com)");
    }
    const baseUrl = options.baseUrl.replace(/\/+$/, "");
    const http = createClient(
      createConfig({
        baseUrl,
        throwOnError: true,
        responseStyle: "data",
        credentials: options.credentials,
        headers: options.headers,
        ...(options.fetch ? { fetch: options.fetch } : {}),
        ...(options.token ? { auth: () => resolveToken(options.token) } : {}),
      }),
    );

    // The transport throws the parsed error body — a plain object with no
    // status and no stack. Replacing it here is what makes `catch (e)` see a
    // real Error everywhere, including inside the generated methods.
    http.interceptors.error.use((error, response) => {
      // No response means the request never completed — a DNS failure, an
      // abort, an offline tab. There is no status to carry, so the transport's
      // own error is already the truthful one.
      if (!response) return error;
      const failure = new TalqingApiError(error as ErrorResponse, response);
      if (failure.status === 401) options.onUnauthorized?.(failure);
      return failure;
    });

    super({ client: http });
    this.baseUrl = baseUrl;
    this.http = http;
  }

  oauthStartUrl(provider: string, integrationId?: string): string {
    const url = new URL(`${this.baseUrl}/v1/integrations/oauth/${encodeURIComponent(provider)}/start`);
    if (integrationId) url.searchParams.set("integration_id", integrationId);
    return url.toString();
  }
}

/* `Throwing` is a type-level narrowing of the same object — the runtime value
 * is the generated class, unchanged — so the constructor is re-typed rather
 * than the 155 methods being re-declared. */
export const TalqingClient = TalqingClientImpl as unknown as new (
  options: TalqingClientOptions,
) => TalqingClient;

async function resolveToken(token: TokenProvider | undefined): Promise<string | undefined> {
  const value = typeof token === "function" ? await token() : token;
  return value ?? undefined;
}

/**
 * Walk every page of a list endpoint.
 *
 *     for await (const agent of paginate((q) => talqing.agents.list(q))) { … }
 *
 * Replaces the one-off `listAllAgents` the previous client carried: the page
 * shape is the same on all 28 list endpoints, so the helper is too.
 */
export async function* paginate<T>(
  page: (query: { limit: number; offset: number }) => Promise<Page<T>>,
  { limit = 200 }: { limit?: number } = {},
): AsyncGenerator<T> {
  for (let offset = 0; ; offset += limit) {
    const result = await page({ limit, offset });
    yield* result.items;
    if (!result.has_more) return;
  }
}
