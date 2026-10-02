"use client";
import type {
  AgentConfig,
  CatalogResponse,
  CostEstimateUsagePerMinute,
  ModelHostResponse,
} from "@talqing/sdk";
import { cn } from "@/lib/cn";
import { LIFT_ON_HOVER, Panel, Tooltip } from "@/app/components/ui";
import {
  avatarEntryFor,
  estimateAvatarPerMinute,
  estimateLlmPerMinute,
  estimateRealtimePerMinute,
  estimateSttPerMinute,
  estimateTtsPerMinute,
  isSearchedProvider,
  moneyPerMin,
  providerDisplayLabel,
  providerModelLabel,
  realtimeEntryFor,
} from "./agentConfig";
import { chosenEndpoints } from "./HostSelect";

// Meter colors keyed by line — shared by the bar segments and the legend dots.
// See the `chart` tokens in tailwind.config.ts for why these are their own
// palette rather than the status colors.
const SEGMENT: Record<string, string> = {
  STT: "bg-chart-stt",
  LLM: "bg-chart-llm",
  TTS: "bg-chart-tts",
  Realtime: "bg-chart-realtime",
  Avatar: "bg-chart-avatar",
  Platform: "bg-chart-platform",
};

/** `upTo`: the cost is a ceiling rather than an estimate of the middle. */
type Line = { key: string; cost: number; sub: string; upTo?: boolean };

/** What a segment or a legend dot says on hover. */
function tip(line: Line, unit: "min" | "message") {
  return (
    <>
      <span className="font-semibold">{line.key}</span>
      <span className="ml-1.5 font-mono tabular-nums">
        {line.upTo && "up to "}
        {moneyPerMin(line.cost, unit)}
      </span>
      <span className="mt-0.5 block text-placeholder">{line.sub}</span>
    </>
  );
}

/** What the current draft costs to run, broken down by stage: per minute for a
 *  call, per answered message for a chat — the unit each one is billed in. */
export function CostEstimate({
  catalog,
  config,
  llmHosts,
}: {
  catalog: CatalogResponse;
  config: AgentConfig;
  /** The primary LLM's hosts (`useModelHosts`), for pricing a host set. */
  llmHosts: ModelHostResponse[] | null;
}) {
  const isText = config.channel === "text";
  const isVideo = config.channel === "video";
  const realtimeEntry = realtimeEntryFor(catalog, config);

  const sttEntry = config.stt
    ? catalog.stt.find((e) => e.provider === config.stt!.provider && e.model === config.stt!.model)
    : null;
  const listedLlm = catalog.llm.find(
    (e) => e.provider === config.llm?.provider && e.model === config.llm?.model,
  );
  // A chat has no minutes: it is billed per answered message, so that is the
  // unit its estimate is quoted in and the usage block it is computed from.
  const unit = isText ? "message" : "min";
  const usage: Partial<CostEstimateUsagePerMinute> = isText
    ? catalog.cost_estimate_usage_per_text_message
    : catalog.cost_estimate_usage_per_minute;
  // A gateway's listed rate is its cheapest host's. With hosts chosen the
  // latency sort ignores price, so any of them may answer: price the dearest,
  // which makes the line a ceiling where Automatic's is a floor.
  const dearest =
    listedLlm && config.llm?.hosts && llmHosts
      ? chosenEndpoints(config.llm.hosts, llmHosts)
          .map((host) => ({ ...listedLlm, pricing: host.pricing }))
          .reduce<typeof listedLlm | null>(
            (worst, entry) =>
              worst && estimateLlmPerMinute(worst, usage) >= estimateLlmPerMinute(entry, usage)
                ? worst
                : entry,
            null,
          )
      : null;
  const llmEntry = dearest ?? listedLlm;
  const ttsEntry = config.tts
    ? catalog.tts.find((e) => e.provider === config.tts!.provider && e.model === config.tts!.model)
    : null;
  const avatarEntry = avatarEntryFor(catalog, config);
  // Named rather than derived from the entry, because there may not be one: a
  // searched provider's models are fetched apart from the catalog, so an entry
  // can be a beat behind the config. Saying "Not configured" about a model the
  // agent is plainly configured with would be the wrong half of the truth.
  const llmLabel = llmEntry
    ? providerModelLabel(catalog, llmEntry)
    : config.llm?.model
      ? `${providerDisplayLabel(catalog, config.llm.provider)} · ${config.llm.model}`
      : "Not configured";
  // Watching a screen is the one setting outside the model picker that moves a
  // line here: one image on every user turn is real input tokens, and enough of
  // them to see. Folded into the LLM line rather than given its own, because
  // that is where the invoice will put them.
  const watchesScreen = config.vision_input?.screenshare?.enabled === true;
  // A gateway's listed rate is its cheapest host's, so any stage it serves makes
  // this a floor rather than an estimate of the middle. Billing uses what it
  // reported per request instead, which is the number the footnote points at.
  const gateway = config.realtime
    ? null
    : !isText && isSearchedProvider(catalog, config.stt?.provider, "stt")
      ? config.stt?.provider
      : isSearchedProvider(catalog, config.llm?.provider, "llm") && !dearest
        ? config.llm?.provider
        : null;

  // An unset channel is voice, the same default the API applies and the same one
  // `isText` / `isVideo` above read it with.
  const platform = isText
    ? catalog.platform_fee_per_message.text
    : catalog.platform_fee_per_minute[isVideo ? "video" : "voice"];
  const llmLine = {
    key: "LLM",
    cost: estimateLlmPerMinute(llmEntry, usage, config.llm?.priority === true, watchesScreen),
    sub: watchesScreen ? `${llmLabel} · incl. screen frames` : llmLabel,
    upTo: dearest !== null,
  };
  // One model in place of three, so it replaces their three lines rather than
  // sitting beside them at zero — a stage that can never bill is noise.
  const stageLines = config.realtime
    ? [
        {
          key: "Realtime",
          cost: estimateRealtimePerMinute(realtimeEntry, usage),
          sub: providerModelLabel(catalog, realtimeEntry),
        },
      ]
    : isText
      ? // A chat runs one model and nothing else, so it has one stage.
        [llmLine]
      : [
          {
            key: "STT",
            cost: estimateSttPerMinute(sttEntry, usage),
            sub: providerModelLabel(catalog, sttEntry),
          },
          llmLine,
          {
            key: "TTS",
            cost: estimateTtsPerMinute(ttsEntry, usage),
            sub: providerModelLabel(catalog, ttsEntry),
          },
        ];
  // Post-call analysis is deliberately absent. Every line here is a rate per
  // minute or per message; analysis is one model call after the session ends,
  // priced on a transcript that does not exist yet. It still bills — as its own `purpose = 'analysis'`
  // usage row, on the tokens it really spent — it just cannot be forecast per
  // minute, and a guessed segment sitting next to five measured ones read as if
  // it were one of them.
  const lines = [
    ...stageLines,
    ...(isText
      ? []
      : [
          {
            key: "Avatar",
            cost: isVideo ? estimateAvatarPerMinute(avatarEntry) : 0,
            sub: isVideo ? providerModelLabel(catalog, avatarEntry) : "Off for voice agents",
          },
        ]),
    {
      key: "Platform",
      cost: platform,
      sub: isText ? "Talqing fee, per answered message" : "Flat talqing fee",
    },
  ];
  // One unknown rate makes the total unknown, and the meter has no honest shape
  // to take — every segment is a share of a number nobody has. Both fall back to
  // "—" and an empty track rather than to a figure that omits a stage.
  const priced = lines.every((line) => Number.isFinite(line.cost));
  const total = priced ? lines.reduce((sum, line) => sum + line.cost, 0) : NaN;
  const billable = priced ? lines.filter((line) => line.cost > 0) : [];

  return (
    <Panel className={cn("p-4", LIFT_ON_HOVER)}>
      <div className="mb-2.5 flex items-baseline justify-between gap-3">
        {/* The same heading a config section wears — this card sits beside them
            and is read the same way. */}
        <h2 className="text-[16px] font-semibold leading-6 text-ink">Cost estimate</h2>
        <span className="font-mono text-[15px] font-semibold tabular-nums text-ink">
          {moneyPerMin(total, unit)}
        </span>
      </div>
      {/* Hidden from the a11y tree on purpose: the meter says nothing the legend
          below does not say in words, and the legend is the keyboard path to the
          same per-stage tooltips. Announcing both reads every stage twice. */}
      <div className="mb-3 flex h-2 w-full rounded-full bg-subtle" aria-hidden>
        {billable.length ? (
          billable.map((line) => (
            <span
              key={line.key}
              className="relative h-full min-w-[6px]"
              // Share of the total, as a flex basis that can shrink — runtime
              // layout, not styling. Not flex-grow: the growth factors would be
              // dollar amounts summing to well under 1, and flexbox then hands out
              // only that fraction of the track, collapsing the meter to a few px.
              style={{ flexBasis: `${(line.cost / total) * 100}%` }}
            >
              {/* The hover target has to be taller than the 8px bar, and a thin
                  segment is only a few px wide, so it reaches past both edges. */}
              <Tooltip
                label={tip(line, unit)}
                className="absolute -inset-y-2 -inset-x-0 items-center"
              >
                {/* One continuous bar: the colours meet, and only the two outer
                    ends are rounded. Clipping the track with overflow-hidden
                    would round them too, but it would also cut off the hover
                    targets above, which reach past the bar on purpose. */}
                <span
                  className={cn(
                    "block h-2 w-full",
                    SEGMENT[line.key] || "bg-line-strong",
                    line.key === billable[0].key && "rounded-l-full",
                    line.key === billable[billable.length - 1].key && "rounded-r-full",
                  )}
                />
              </Tooltip>
            </span>
          ))
        ) : (
          <span className="h-full w-full rounded-full bg-line" />
        )}
      </div>
      <div className="flex flex-wrap gap-x-3.5 gap-y-1.5" aria-label="Cost estimate legend">
        {lines.map((line) => (
          <Tooltip
            key={line.key}
            label={tip(line, unit)}
            className={cn(
              "items-center gap-1.5 rounded text-[12px] focus:outline-none focus-visible:ring-2 focus-visible:ring-ink/15",
              line.cost ? "text-ink-soft" : "text-faint",
            )}
          >
            <span
              className={cn(
                "mr-1.5 h-2 w-2 flex-none self-center rounded-full",
                SEGMENT[line.key] || "bg-line-strong",
                !line.cost && "opacity-35",
              )}
              aria-hidden
            />
            {line.key}
          </Tooltip>
        ))}
      </div>
      {isText && (
        <p className="mt-2.5 text-[11.5px] leading-[1.45] text-faint">
          Assumes {usage.llm_input_tokens} tokens in and {usage.llm_output_tokens} out per message.
          A long chat sends more.
        </p>
      )}
      {config.realtime && (
        // Audio tokens are what a realtime model actually bills, and how many
        // there are depends on how much of the minute each side speaks for.
        // Say so rather than let this read as a measured figure.
        <p className="mt-2.5 text-[11.5px] leading-[1.45] text-faint">
          Assumes the model hears the whole minute and speaks for about half of it.
        </p>
      )}
      {watchesScreen && (
        // The frame count is bounded by turns, not by seconds, so unlike every
        // other line here this one depends on how talkative the call is. Say so
        // rather than let it read as a measured figure.
        <p className="mt-2.5 text-[11.5px] leading-[1.45] text-faint">
          Screen share assumes about two and a half turns a minute, each carrying one frame.
        </p>
      )}
      {gateway && (
        <p className="mt-2.5 text-[11.5px] leading-[1.45] text-faint">
          {providerDisplayLabel(catalog, gateway)} is priced at its cheapest host&rsquo;s rates, so
          this is a floor. The invoice uses what it actually charged.
        </p>
      )}
      {dearest && (
        <p className="mt-2.5 text-[11.5px] leading-[1.45] text-faint">
          LLM priced at the dearest host you allowed, so this is a ceiling.
        </p>
      )}
      {sttEntry?.streaming === false && (
        // The batch rate is charged per second of audio submitted, and only the
        // caller's own utterances get submitted — so unlike every other line here
        // it depends on how much of the minute the caller actually talks. Say so
        // rather than let this read as a measured figure.
        <p className="mt-2.5 text-[11.5px] leading-[1.45] text-faint">
          Batch speech-to-text assumes {usage.stt_batch_audio_seconds ?? 0}s of speech per minute.
        </p>
      )}
    </Panel>
  );
}
