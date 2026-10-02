import { RpcError, type Room, type RpcInvocationData } from "livekit-client";
import type { JsonValue } from "./json.js";

/* ── frontend RPC ─────────────────────────────────────────────────────────
 *
 * A tool's `frontend_rpc` operation calls back into the page mid-call. The
 * agent RPCs ONE method — `talqing.frontend_rpc` — carrying an envelope that
 * names the method the page should run, so a page registers once and dispatches
 * on `envelope.method` rather than registering a room method per tool.
 */

export interface TalqingRpcInvocation {
  payload: string;
  responseTimeout?: number;
  callerIdentity?: string;
}

export type TalqingRpcHandler = (
  data: TalqingRpcInvocation,
) => string | Promise<string>;
export type TalqingRpcHandlers = Record<string, TalqingRpcHandler>;

export interface TalqingFrontendRpcEnvelope {
  method: string;
  payload?: JsonValue;
}

/** Throw this from a handler to choose the code the agent sees. */
export class TalqingRpcError extends Error {
  readonly code: number;
  readonly data?: string;

  constructor(code: number, message: string, data?: string) {
    super(message);
    this.name = "TalqingRpcError";
    this.code = code;
    this.data = data;
  }
}

/** Register the frontend-RPC envelope handler on a room. Returns the unregister.
 *
 *  A `"*"` key catches every method with no handler of its own, and receives the
 *  WHOLE envelope as its payload string so it can tell what it caught. */
export function registerTalqingRpcHandlers(
  room: Room,
  handlers: TalqingRpcHandlers,
): () => void {
  room.registerRpcMethod("talqing.frontend_rpc", async (data: RpcInvocationData) => {
    try {
      const envelope = JSON.parse(data.payload || "{}") as TalqingFrontendRpcEnvelope;
      const isWildcard = handlers[envelope.method] === undefined;
      const handler = handlers[envelope.method] ?? handlers["*"];
      if (!handler) {
        throw new TalqingRpcError(
          1404,
          `No frontend RPC handler registered for ${envelope.method}`,
        );
      }
      return await handler({
        payload: isWildcard
          ? JSON.stringify(envelope)
          : envelope.payload === undefined
            ? "{}"
            : JSON.stringify(envelope.payload),
        responseTimeout: data.responseTimeout,
        callerIdentity: data.callerIdentity,
      });
    } catch (error) {
      if (error instanceof TalqingRpcError) {
        throw new RpcError(error.code, error.message, error.data);
      }
      throw new RpcError(
        1500,
        error instanceof Error ? error.message : "RPC handler failed",
      );
    }
  });

  return () => room.unregisterRpcMethod("talqing.frontend_rpc");
}
