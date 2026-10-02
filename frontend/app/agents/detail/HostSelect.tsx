"use client";

import { useEffect, useRef, useState } from "react";

import type { CatalogResponse, ModelHostResponse } from "@talqing/sdk";

import { api } from "@/lib/api";

import { Label, Select } from "../../components/ui";

import { isSearchedProvider } from "./agentConfig";
import { priceLine } from "./ModelSearch";

/* Which of OpenRouter's upstream hosts may serve one language model. OpenRouter
   calls them "providers"; here that word already means the catalog provider
   (`llm.provider = "openrouter"`), so they are hosts everywhere. */

/** Stands for "no host in particular" inside the multi-choice `Select`, whose
 *  value is a list — it never reaches the config, where Automatic is `null`. */
const AUTOMATIC = "__automatic__";

/** Tag suffixes naming a service tier. A bare base tag does not match these —
 *  OpenRouter routes to a tier only when it is named in full. */
const SERVICE_TIERS = new Set(["flex", "priority", "fast"]);

const SEGMENT_LABELS: Record<string, string> = {
  turbo: "Turbo",
  us: "US",
  eu: "EU",
  global: "Global",
  priority: "Priority",
  fast: "Fast",
};

/** A tag's variant segments, readable: `deepinfra/fp8` → ["FP8"]. Keeping the
 *  quantization is what stops `deepinfra/bf16` and `deepinfra/fp8` both reading
 *  "DeepInfra". */
function segments(tag: string): string[] {
  return tag
    .split("/")
    .slice(1)
    .map((part) => SEGMENT_LABELS[part] ?? (/^(fp|bf|int)\d+$/.test(part) ? part.toUpperCase() : part));
}

function hostLabel(host: ModelHostResponse): string {
  if (host.all_endpoints) return `${host.name} · All endpoints`;
  return [host.name, ...segments(host.host)].join(" · ");
}

/** What a sentence calls one host: an all-endpoints row is just the host. */
function hostName(tag: string, hosts: ModelHostResponse[] | null): string {
  const host = hosts?.find((h) => h.host === tag);
  if (!host) return tag;
  return host.all_endpoints ? host.name : hostLabel(host);
}

/** The facts that decide between hosts: speed first, then price, then a warning
 *  only when the last half hour was shaky. Latency and throughput are absent
 *  without a workspace OpenRouter key, which only an authenticated request is
 *  given. */
function hostDetail(host: ModelHostResponse): string {
  const quantization = host.quantization?.toUpperCase();
  const labelled = segments(host.host).includes(quantization ?? "");
  return [
    quantization && !labelled ? quantization : null,
    host.latency_p50_ms != null ? `${host.latency_p50_ms} ms` : null,
    host.throughput_p50 != null ? `${host.throughput_p50} tok/s` : null,
    priceLine(host.pricing),
    host.uptime_30m != null && host.uptime_30m < 95 ? `${Math.round(host.uptime_30m)}% uptime` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** The base tag this row is implied by, when that base tag is chosen: `azure/eu`
 *  while `azure` (All endpoints) is checked. */
function impliedBy(tag: string, chosen: string[], hosts: ModelHostResponse[]): string | null {
  const [base, ...rest] = tag.split("/");
  if (!rest.length || SERVICE_TIERS.has(rest[rest.length - 1])) return null;
  const covering = hosts.find((h) => h.all_endpoints && h.host === base && chosen.includes(base));
  return covering ? covering.host : null;
}

/** The endpoints a host set can land on: each chosen tag, and for an
 *  all-endpoints base tag every endpoint of that host it matches. Unknown tags
 *  are skipped — OpenRouter skips them too. */
export function chosenEndpoints(
  chosen: string[],
  hosts: ModelHostResponse[],
): ModelHostResponse[] {
  return hosts.filter((h) => chosen.includes(h.host) || impliedBy(h.host, chosen, hosts) !== null);
}

/** One model's hosts, fetched when the model changes. Null while loading, and
 *  for anything but a searched provider's language model. A slower answer for
 *  a model the author has already moved off is dropped. */
export function useModelHosts(
  catalog: CatalogResponse | null,
  spec: { provider?: string; model?: string } | null | undefined,
): ModelHostResponse[] | null {
  const model = catalog && isSearchedProvider(catalog, spec?.provider, "llm") ? spec?.model || "" : "";
  const [hosts, setHosts] = useState<{ model: string; hosts: ModelHostResponse[] } | null>(null);
  const latest = useRef(0);

  useEffect(() => {
    if (!model) return;
    const seq = ++latest.current;
    void api
      .listModelHosts({ model })
      .then((page) => {
        if (seq === latest.current) setHosts({ model, hosts: page.hosts });
      })
      .catch(() => {
        /* Left loading. The control still shows Automatic and whatever is
           stored, and publish validation checks the set against OpenRouter. */
      });
  }, [model]);

  return hosts && hosts.model === model ? hosts.hosts : null;
}

/** Which hosts may serve one OpenRouter model. Renders nothing for any other
 *  provider, or before a model is chosen. `null` is Automatic; an empty list
 *  never leaves here. */
export function HostSelect({
  catalog,
  spec,
  hosts,
  onChange,
  ariaLabel = "Hosts",
}: {
  catalog: CatalogResponse;
  spec: { provider?: string; model?: string; hosts?: string[] | null } | null | undefined;
  /** From `useModelHosts`, called by the parent so the cost estimate shares it. */
  hosts: ModelHostResponse[] | null;
  onChange: (hosts: string[] | null) => void;
  ariaLabel?: string;
}) {
  if (!isSearchedProvider(catalog, spec?.provider, "llm") || !spec?.model) return null;

  const chosen = spec.hosts ?? [];
  const known = new Set(hosts?.map((h) => h.host) ?? []);
  // Only once the list is in: before that, every stored host would read as gone.
  const stale = hosts ? chosen.filter((tag) => !known.has(tag)) : [];
  const loading = hosts === null ? chosen : [];
  const available = stale.length ? "Available" : undefined;
  const logo = catalog.providers?.[spec.provider ?? ""]?.logo_url;

  return (
    // Always the model's column in `MODEL_HOSTS_GRID`, whether or not a
    // thinking control sits before it.
    <div className="flex min-w-0 flex-col gap-2 md:col-start-2">
      <Label>Hosts</Label>
      <Select
        multiple
        value={spec.hosts ?? [AUTOMATIC]}
        onChange={(e) => {
          const picked = e.target.value.filter((v) => v !== AUTOMATIC);
          // A base tag checked over its own variants keeps just the base: it
          // already matches them, and listing both would say so twice.
          const next = hosts ? picked.filter((tag) => impliedBy(tag, picked, hosts) === null) : picked;
          onChange(next.length ? next : null);
        }}
        aria-label={ariaLabel}
        popupClassName="max-w-[480px]"
      >
        {stale.length > 0 && (
          <optgroup label="No longer available">
            {stale.map((tag) => (
              <option key={tag} value={tag}>
                {tag}
              </option>
            ))}
          </optgroup>
        )}
        <optgroup label={available}>
          <option
            value={AUTOMATIC}
            data-exclusive
            data-logo-url={logo}
            data-detail="Any host; the fastest answers each request"
          >
            Automatic
          </option>
          {loading.map((tag) => (
            <option key={tag} value={tag}>
              {tag}
            </option>
          ))}
          {hosts?.map((host) => {
            const implied = impliedBy(host.host, chosen, hosts) !== null;
            return (
              <option
                key={host.host}
                value={host.host}
                disabled={implied}
                data-checked={implied || undefined}
                data-logo-url={host.logo_url ?? undefined}
                data-detail={hostDetail(host)}
              >
                {hostLabel(host)}
              </option>
            );
          })}
        </optgroup>
      </Select>
    </div>
  );
}

/** The one host-set risk worth a line: a single host and nothing behind it. */
export function HostNote({
  value,
  hosts,
  subject,
  failover,
}: {
  value: string[] | null | undefined;
  hosts: ModelHostResponse[] | null;
  /** What fails when the host does: "the turn", "the run", "the analysis". */
  subject: string;
  /** Whether a fallback model covers this one. */
  failover: "present" | "absent" | "impossible";
}) {
  if (value?.length !== 1 || failover === "present") return null;
  return (
    <p className="mt-3 text-[13px] leading-5 text-muted">
      If {hostName(value[0], hosts)} goes down, {subject} fails.
    </p>
  );
}
