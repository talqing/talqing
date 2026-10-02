"use client";

import { useMemo, useState } from "react";
import { Button, Field, Input, Modal, Select } from "@/app/components/ui";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import type { AgentResponse, StreamConnectionResponse } from "@talqing/sdk";
import { DIALECTS, DIALECT_ORDER, agentLabel, type StreamDialect } from "./shared";

/** Three decisions, and the third one is the only irreversible-feeling part.
 *
 *  Not a wizard: a connection is a name, a platform and an agent, and every one
 *  of them is changeable afterwards except the platform. The URL that comes back
 *  is what makes this feel like a commitment, and it appears in the row's setup
 *  panel where the partner's instructions are — rather than in a second dialog
 *  that has to be dismissed before the instructions can be read. */
export function CreateConnection({
  agents,
  existing,
  onClose,
  onCreated,
}: {
  /** Every voice agent, published or not — see `isVoiceAgent`. */
  agents: AgentResponse[];
  existing: StreamConnectionResponse[];
  onClose: () => void;
  onCreated: (connection: StreamConnectionResponse) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [dialect, setDialect] = useState<StreamDialect>("sparktg");
  const [agentId, setAgentId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  /* The URL names the platform and the agent, so an agent has at most one
     connection per platform — a unique index on the server. Saying so here turns
     a 409 into a missing option. */
  const available = useMemo(() => {
    const taken = new Set(
      existing
        .filter((connection) => connection.dialect === dialect)
        .map((connection) => connection.agent_id),
    );
    return agents.filter((agent) => !taken.has(agent.id));
  }, [agents, existing, dialect]);
  /* Switching platform can take the picked agent off the list; it is then no
     longer picked, rather than a value the select cannot show. */
  const pickedAgentId = available.some((agent) => agent.id === agentId) ? agentId : "";
  const spec = DIALECTS[dialect];

  async function submit() {
    setSaving(true);
    setError("");
    try {
      await onCreated(
        await api.createStreamConnection({ name: name.trim(), dialect, agent_id: pickedAgentId }),
      );
    } catch (err: unknown) {
      setError(apiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  const ready = name.trim().length > 0 && pickedAgentId.length > 0;

  return (
    <Modal
      title="New media stream"
      sub="One URL, one agent. The partner configures it once on their side and nothing about their numbers moves."
      width="max-w-[560px]"
      onClose={() => !saving && onClose()}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={!ready || saving}>
            {saving ? "Creating…" : "Create connection"}
          </Button>
        </>
      }
    >
      <div className="grid gap-3.5 pb-2">
        <Field label="Name" hint="What this partner integration is called here. Only you see it.">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="SparkTG — Acme support line"
          />
        </Field>

        <Field
          label="Their platform"
          hint="It cannot be changed afterwards: the URL names it, and the partner has already configured it by then."
        >
          <Select value={dialect} onChange={(e) => setDialect(e.target.value as StreamDialect)}>
            {DIALECT_ORDER.map((value) => (
              <option key={value} value={value}>
                {DIALECTS[value].label}
              </option>
            ))}
          </Select>
        </Field>

        <p className="-mt-1 text-[13px] leading-5 text-muted">{spec.connects}</p>

        <Field
          label="Answered by"
          hint="A voice agent. Each connection answers as exactly one, and an unpublished one can be wired up now and published later — the connection just says No agent until it is."
        >
          <Select value={pickedAgentId} onChange={(e) => setAgentId(e.target.value)}>
            <option value="">Pick an agent…</option>
            {available.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agentLabel(agent)}
              </option>
            ))}
          </Select>
        </Field>

        {agents.length === 0 && (
          <p className="text-[13px] leading-5 text-warn">
            No voice agents. A stream carries audio, so it needs one — create an agent first.
          </p>
        )}
        {agents.length > 0 && available.length === 0 && (
          <p className="text-[13px] leading-5 text-warn">
            Every voice agent already has a {spec.label} connection. The URL names the platform
            and the agent, so a second one would only ever be the same URL again.
          </p>
        )}

        {/* The two things a reader should know before the partner is told
            anything, because both are discovered in UAT otherwise. */}
        <ul className="grid gap-1.5 rounded-lg border border-line bg-canvas px-3.5 py-3 text-[12.5px] leading-5 text-ink-soft">
          {!spec.canHangup && (
            <li>
              On {spec.label}, the agent finishing hands the caller back to the partner&apos;s own
              flow. It does not hang up — make sure they have something after the stream.
            </li>
          )}
          {!spec.sendsCallerNumber && (
            <li>
              {spec.label} sends no caller number on the stream. The partner has to pass one, or
              every call is anonymous and no caller gets their history back.
            </li>
          )}
          <li>
            A streamed agent cannot transfer to a human: the partner owns the phone line, so
            escalation has to be their flow&apos;s job.
          </li>
        </ul>

        {error && (
          <p className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3.5 py-2.5 text-[13px] leading-5 text-danger [overflow-wrap:anywhere]">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
