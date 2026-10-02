"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { Button, Field, Input, Label, Modal, Select } from "@/app/components/ui";
import { KVRows } from "@/app/components/KVRows";
import {
  SessionVarFields,
  missingVars,
  missingVarsSentence,
  suppliedVars,
  usePublishedVars,
} from "@/app/components/SessionVars";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import type { AgentResponse, JsonObject, PhoneNumberResponse } from "@talqing/sdk";
import { agentLabel } from "./shared";

export function TestCallModal({
  number,
  agents,
  onClose,
}: {
  number: PhoneNumberResponse;
  /** Published voice agents only — nothing else can be dispatched onto SIP. */
  agents: AgentResponse[];
  onClose: () => void;
}) {
  // Defaults to whoever answers this number, which is almost always who you
  // want to hear on a test call out of it.
  const [agentId, setAgentId] = useState(number.inbound_agent_id || agents[0]?.id || "");
  const [to, setTo] = useState("");
  // Opens on one blank row: typing a variable should not cost a click first.
  const [userdata, setUserdata] = useState<JsonObject>({ "": "" });
  const [varValues, setVarValues] = useState<Record<string, string>>({});
  const [calling, setCalling] = useState(false);
  const [sessionId, setSessionId] = useState("");
  const [err, setErr] = useState("");
  // Off the PUBLISHED version, which is what this call runs — `agents[].config`
  // is the draft, so it would show a list the API does not enforce.
  const { declared, loading: loadingVars, error: varsError } = usePublishedVars(agentId, agents);
  const missing = missingVars(declared, varValues);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setCalling(true);
    setErr("");
    try {
      // A half-typed row is a row the author has not finished, not an error to
      // throw back at them — the call goes out without it.
      const seed = Object.fromEntries(
        Object.entries(userdata)
          .map(([k, v]) => [k.trim(), v] as const)
          .filter(([k]) => k),
      );
      const supplied = suppliedVars(declared, varValues);
      const result = await api.createOutboundCall({
        agent_id: agentId,
        from_phone_number_id: number.id,
        to: to.trim(),
        userdata: Object.keys(seed).length ? seed : null,
        vars: Object.keys(supplied).length ? supplied : null,
      });
      setSessionId(result.session_id);
    } catch (error: unknown) {
      setErr(apiErrorMessage(error));
    } finally {
      setCalling(false);
    }
  }

  return (
    <Modal
      title="Place a test call"
      sub={`Dial out from ${number.e164}.`}
      width="max-w-[520px]"
      onClose={() => {
        if (calling) return;
        onClose();
      }}
      footer={
        sessionId ? (
          <>
            <Button variant="secondary" onClick={onClose}>
              Close
            </Button>
            <Link href={`/calls?id=${sessionId}`}>
              <Button>View call</Button>
            </Link>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose} disabled={calling}>
              Cancel
            </Button>
            <Button
              type="submit"
              form="test-call-form"
              disabled={
                calling ||
                loadingVars ||
                Boolean(varsError) ||
                !agentId ||
                !to.trim() ||
                missing.length > 0
              }
            >
              {calling ? "Calling…" : "Call"}
            </Button>
          </>
        )
      }
    >
      <form id="test-call-form" className="grid gap-4 pb-6" onSubmit={(e) => void submit(e)}>
        {err && (
          <div className="rounded-md border border-danger/30 bg-danger/[0.06] px-3.5 py-2.5 text-[13.5px] text-danger [overflow-wrap:anywhere]">
            {err}
          </div>
        )}
        <Field label="To" hint="Destination in E.164 format, e.g. +919876543210">
          <Input
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="+91…"
            className="font-mono"
            disabled={Boolean(sessionId)}
            required
          />
        </Field>
        <Field label="Agent">
          <Select
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            disabled={Boolean(sessionId) || agents.length === 0}
          >
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agentLabel(agent)}
              </option>
            ))}
          </Select>
        </Field>
        {declared.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <Label>Variables</Label>
            <SessionVarFields
              declared={declared}
              values={varValues}
              onChange={setVarValues}
              idPrefix="test-call-var"
              disabled={Boolean(sessionId)}
            />
            <p className="text-[13px] leading-5 text-muted">
              What this call knows about the deployment rather than about the person. The agent
              reads them as{" "}
              <code className="font-mono text-[12.5px] text-ink">{"{{vars.name}}"}</code> in its
              prompt, greeting and tools, and they are gone when the call ends.
            </p>
          </div>
        )}
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between gap-3">
            <Label optional>User data</Label>
            {!sessionId && (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => setUserdata({ ...userdata, "": "" })}
              >
                + variable
              </Button>
            )}
          </div>
          <KVRows
            obj={userdata}
            onChange={setUserdata}
            disabled={Boolean(sessionId)}
            className=""
          />
          <p className="text-[13px] leading-5 text-muted">
            What the call already knows about the person you are dialling. The agent reads it as{" "}
            <code className="font-mono text-[12.5px] text-ink">{"{{userdata.key}}"}</code> in its
            prompt and greeting, and its tools receive it too.
          </p>
        </div>
        {varsError && <p className="text-[13px] leading-5 text-danger">{varsError}</p>}
        {missing.length > 0 && !sessionId && (
          <p className="text-[13px] leading-5 text-warn">{missingVarsSentence(missing)}</p>
        )}
        {sessionId && (
          <div className="rounded-lg border border-live/25 bg-live/[0.06] px-3.5 py-3 text-[13.5px] text-live">
            Call queued.{" "}
            <Link href={`/calls?id=${sessionId}`} className="font-semibold underline">
              Open the session
            </Link>
          </div>
        )}
      </form>
    </Modal>
  );
}
