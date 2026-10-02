"use client";
import { useEffect, useState } from "react";
import type { AgentResponse, VarDeclaration } from "@talqing/sdk";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { cn } from "@/lib/cn";
import { Input, Label } from "./ui";

/* Supplying `{{vars.*}}` on the five surfaces that start something: a test call,
   a test chat, a test dial, a campaign, and the task Run panel.

   A declared variable is not a key/value pair. We know its name, what it is for
   and what it falls back to, and a free-form grid throws all three away — so
   this renders one labelled field per declaration, the description under it and
   the default as the placeholder.

   One component for all five, for the same reason `VariablesSection` is one
   component for an agent and a task: they edit one shape, and two copies of it
   are how they drift. */

/** The variables a session-start form should ask for: the PUBLISHED agent's.
 *
 *  `AgentResponse.config` is the DRAFT, and every one of these surfaces starts
 *  the published agent — so reading declarations off it is the bug that looks
 *  right in the editor and refuses in production. */
export function usePublishedVars(
  agentId: string | null,
  agents: AgentResponse[],
): { declared: VarDeclaration[]; loading: boolean; error: string } {
  const [declared, setDeclared] = useState<VarDeclaration[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const version = agents.find((a) => a.id === agentId)?.published_version ?? null;

  useEffect(() => {
    setError("");
    if (!agentId || version === null) {
      setDeclared([]);
      setLoading(false);
      return;
    }
    // The agent can be re-picked while a fetch is in flight, and the slower
    // answer must not overwrite the newer one.
    let live = true;
    setLoading(true);
    api
      .getAgentVersion(agentId, version)
      .then((v) => live && setDeclared(v.config.vars ?? []))
      .catch((e: unknown) => {
        // Said out loud rather than left as an empty list: a form that quietly
        // drops a required field looks right here and is refused by the API.
        if (!live) return;
        setDeclared([]);
        setError(apiErrorMessage(e, "Could not read this agent's published variables."));
      })
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [agentId, version]);

  return { declared, loading, error };
}

/** The required variables this form has not filled in.
 *
 *  Empty rather than absent is the test, because an untouched box is not a
 *  value: `suppliedVars` leaves it out entirely, so the API would refuse the
 *  request. A required variable WITH a default is never missing — it resolves
 *  to the default — so it is not flagged. */
export function missingVars(
  declared: VarDeclaration[],
  values: Record<string, string>,
): string[] {
  return declared
    .filter((v) => v.required && v.default === null && !(values[v.name] ?? "").length)
    .map((v) => v.name);
}

/** What to send: only what was typed.
 *
 *  An untouched box is not an empty string — the variable falls back to its
 *  declared default, and sending "" would deliberately blank it instead. */
export function suppliedVars(
  declared: VarDeclaration[],
  values: Record<string, string>,
): Record<string, string> {
  return Object.fromEntries(
    declared
      .map((v) => [v.name, values[v.name]] as const)
      .filter(([, value]) => value !== undefined && value !== ""),
  );
}

/** What to say beside a start button that `missingVars` has disabled. */
export function missingVarsSentence(missing: string[]): string {
  const one = missing.length === 1;
  return `${missing.join(", ")} ${one ? "is" : "are"} required and ${one ? "has" : "have"} no default.`;
}

export function SessionVarFields({
  declared,
  values,
  onChange,
  /** Unique on the page, so two of these can be open at once. */
  idPrefix,
  disabled = false,
  className,
}: {
  declared: VarDeclaration[];
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  idPrefix: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("grid gap-3", className)}>
      {declared.map((v) => (
        <div key={v.name} className="flex min-w-0 flex-col gap-1.5">
          <div className="flex items-baseline gap-2">
            <Label htmlFor={`${idPrefix}-${v.name}`} className="font-mono">
              {v.name}
            </Label>
            {v.required && v.default === null && (
              <span className="text-[11.5px] leading-4 text-warn">needed</span>
            )}
          </div>
          <Input
            id={`${idPrefix}-${v.name}`}
            value={values[v.name] ?? ""}
            // The default is a preview of what happens if this is left alone.
            // With none, "Empty" is that preview — but only when leaving it
            // alone is allowed; on a required one it would contradict the chip
            // right above it, which says the opposite.
            placeholder={v.default ?? (v.required ? "" : "Empty")}
            disabled={disabled}
            onChange={(e) => onChange({ ...values, [v.name]: e.target.value })}
          />
          {v.description && (
            <span className="text-[11.5px] leading-4 text-muted">{v.description}</span>
          )}
        </div>
      ))}
    </div>
  );
}
