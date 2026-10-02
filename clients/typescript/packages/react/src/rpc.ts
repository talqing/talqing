"use client";

import { useEffect, useMemo, useState } from "react";
import {
  registerTalqingRpcHandlers,
  type JsonValue,
  type TalqingRpcHandlers,
  type TalqingRpcInvocation,
} from "@talqing/client";
import { useTalqingConnection } from "./session.js";

/** Handle the agent's `frontend_rpc` calls into this page.
 *
 *  Registration waits for the room to connect and is undone on unmount. Pass
 *  `enabled: false` to hold off. */
export function useTalqingRpcHandlers(handlers: TalqingRpcHandlers, enabled = true) {
  const { room, isConnected } = useTalqingConnection();

  useEffect(() => {
    if (!enabled || !room || !isConnected) return;
    return registerTalqingRpcHandlers(room, handlers);
  }, [enabled, handlers, isConnected, room]);
}

/** The declarative alternative: register nothing, render what arrived.
 *
 *  Keeps the latest payload per method name and always answers the agent "ok". */
export function useTalqingFrontendRpcs(enabled = true) {
  const [actions, setActions] = useState<
    Record<string, { payload?: JsonValue; at: number }>
  >({});

  const handlers = useMemo<TalqingRpcHandlers>(
    () => ({
      "*": async (data: TalqingRpcInvocation) => {
        const payload = JSON.parse(data.payload || "{}") as JsonValue;
        const method = String((payload as { method?: string }).method || "");
        if (method) {
          setActions(
            (current: Record<string, { payload?: JsonValue; at: number }>) => ({
              ...current,
              [method]: {
                payload: (payload as { payload?: JsonValue }).payload,
                at: Date.now(),
              },
            }),
          );
        }
        return "ok";
      },
    }),
    [],
  );

  useTalqingRpcHandlers(handlers, enabled);

  return {
    actions,
    clear: () => setActions({}),
  };
}
