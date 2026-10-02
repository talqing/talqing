"use client";

import { useMemo } from "react";
import {
  getTalqingUserdata,
  setTalqingUserdata,
  type JsonObject,
} from "@talqing/client";
import { useTalqingConnection } from "./session.js";

/** Read and write the live session's userdata from inside a provider. */
export function useTalqingUserdata() {
  const { room } = useTalqingConnection();
  return useMemo(
    () => ({
      get: (keys?: string[], responseTimeout?: number) => {
        if (!room) throw new Error("room is not connected");
        return getTalqingUserdata(room, keys, responseTimeout);
      },
      set: (patch: JsonObject, responseTimeout?: number) => {
        if (!room) throw new Error("room is not connected");
        return setTalqingUserdata(room, patch, responseTimeout);
      },
    }),
    [room],
  );
}
