import type { BadgeVariant } from "@/app/components/ui";
import type { AgentResponse, StreamReadiness } from "@talqing/sdk";

export type StreamDialect = "sparktg" | "twilio" | "plivo" | "exotel" | "vonage";

/* Presentation for the state the API computes. Readiness arrives decided, so
   nothing here re-reads `status` to second-guess it. */
export const READINESS: Record<
  StreamReadiness,
  { label: string; variant: BadgeVariant; hint: string }
> = {
  live: { label: "Live", variant: "live", hint: "Answering calls on this URL." },
  needs_agent: {
    label: "No agent",
    variant: "warn",
    hint: "The agent has never been published, so it cannot answer calls yet.",
  },
  disabled: {
    label: "Disabled",
    variant: "default",
    hint: "New calls are refused. Existing ones were left alone.",
  },
};

/** States a person has to act on. `disabled` was somebody's decision. */
export function needsAttention(readiness: StreamReadiness): boolean {
  return readiness === "needs_agent";
}

/* ── what each platform can and cannot do ───────────────────────────────────
   The one place the differences between the five are written down for a reader.
   Every line here is a real consequence a partner meets in UAT, and the two
   that matter most are the ones nobody expects: on four of the five, the agent
   finishing HANDS THE CALLER BACK to the partner's flow rather than hanging up;
   and Twilio's stream carries no caller number at all. */

export type DialectSpec = {
  label: string;
  /** How the partner points a call at us, in their own vocabulary. */
  connects: string;
  /** Ordered setup steps, written for whoever configures the partner's side. */
  steps: string[];
  /** A snippet to paste, where the partner writes markup rather than clicking. */
  snippet?: { language: string; body: (url: string) => string; caption: string };
  /** Can the agent end the CALL, or only stop streaming? */
  canHangup: boolean;
  /** Can the agent press keys on the caller's line? */
  canSendDtmf: boolean;
  /** Does the protocol carry the caller's number without the partner adding it? */
  sendsCallerNumber: boolean;
  /** The audio this platform speaks, for the reader who has to match it. */
  audio: string;
};

export const DIALECTS: Record<StreamDialect, DialectSpec> = {
  sparktg: {
    label: "SparkTG",
    connects: "Their gateway dials your URL once per call, at the moment the call enters the platform.",
    steps: [
      "Send the URL above to your SparkTG account manager — it is configured per tenant at onboarding, not by you.",
      "Agree which SIP headers become per-call attributes. Every one of them reaches your prompt as {{vars.name}}.",
      "Confirm the codec on your account is slin16k, which is their verified default and the one Talqing is built against.",
    ],
    canHangup: true,
    canSendDtmf: true,
    sendsCallerNumber: true,
    audio: "16 kHz linear PCM (slin16k), announced per call",
  },
  twilio: {
    label: "Twilio",
    connects: "A <Connect><Stream> in the TwiML your voice webhook returns.",
    steps: [
      "Paste the TwiML below into a TwiML Bin and point your number's voice webhook at it — the Bin fills in {{From}}, {{To}} and {{Direction}} on every call. Returning it from your own webhook instead? Substitute those three from Twilio's request yourself.",
      "Keep all three parameters. The stream itself carries no numbers, so without from every call is anonymous and no caller gets their history back — and without direction, every call you place is filed under your own number.",
      "<Hangup/> is what runs once the agent finishes, and with nothing after </Connect> Twilio hangs up anyway. Replace it with a <Dial> or <Redirect> to hand the caller back to your own flow.",
    ],
    snippet: {
      language: "xml",
      caption: "Paste this into a TwiML Bin",
      body: (url) =>
        [
          '<?xml version="1.0" encoding="UTF-8"?>',
          "<Response>",
          "  <Connect>",
          `    <Stream url="${url}">`,
          '      <Parameter name="from" value="{{From}}"/>',
          '      <Parameter name="to" value="{{To}}"/>',
          '      <Parameter name="direction" value="{{Direction}}"/>',
          "    </Stream>",
          "  </Connect>",
          "  <Hangup/>",
          "</Response>",
        ].join("\n"),
    },
    canHangup: false,
    canSendDtmf: false,
    sendsCallerNumber: false,
    audio: "G.711 μ-law at 8 kHz — the only format this protocol has",
  },
  plivo: {
    label: "Plivo",
    connects: "A <Stream bidirectional=\"true\"> in the XML your answer URL returns.",
    steps: [
      "Return the XML below from your number's answer URL — it carries the URL already.",
      "Keep bidirectional=\"true\" and keepCallAlive=\"true\" — without the second one the call moves on to the next element while the agent is still talking.",
      "Fill in extraHeaders from your answer URL's request: from and to as digits with their country code and no + (Plivo allows only letters and digits in a value), and direction as inbound — or outbound on a call you place. The socket itself carries no numbers: without from every call is anonymous, and without direction every call you place is filed under your own number. Plivo does not template this XML for you.",
    ],
    snippet: {
      language: "xml",
      caption: "Return this from your number's answer URL (substitute the two placeholders)",
      body: (url) =>
        [
          "<Response>",
          '  <Stream bidirectional="true" keepCallAlive="true"',
          '          contentType="audio/x-mulaw;rate=8000"',
          '          extraHeaders="from=CALLER_NUMBER;to=DIALLED_NUMBER;direction=inbound">',
          `    ${url}`,
          "  </Stream>",
          "  <Hangup/>",
          "</Response>",
        ].join("\n"),
    },
    canHangup: false,
    canSendDtmf: true,
    sendsCallerNumber: false,
    audio: "Whatever contentType you set — μ-law 8 kHz, or linear PCM at 8 or 16 kHz",
  },
  exotel: {
    label: "Exotel",
    connects: "A Voicebot applet in your call flow, pointed at the URL.",
    steps: [
      "Add a Voicebot applet to the flow on your ExoPhone and paste the URL above into it.",
      "Set the sample rate with ?sample-rate=16000 on the applet's URL if you want more than 8 kHz.",
      "Configure the applet that follows it. The stream closes before the next applet runs, so the agent finishing moves the caller on rather than hanging up.",
    ],
    canHangup: false,
    canSendDtmf: false,
    sendsCallerNumber: true,
    audio: "16-bit linear PCM at 8 kHz, or 16/24 kHz via ?sample-rate=",
  },
  vonage: {
    label: "Vonage",
    connects: "A connect action in the NCCO your answer URL returns.",
    steps: [
      "Return the NCCO below from your number's answer URL — it carries the URL already.",
      "Fill in headers from the answer webhook: from and to exactly as it sends them, and direction as inbound — or outbound on a call you place. The socket carries no numbers: without from every call is anonymous, and without direction every call you place is filed under your own number. Vonage does not substitute these for you.",
      "Keep an eventUrl on the call. Why a socket closed reaches that webhook and never the socket itself.",
    ],
    snippet: {
      language: "json",
      caption: "Return this from your number's answer URL (substitute the two placeholders)",
      body: (url) =>
        JSON.stringify(
          [
            {
              action: "connect",
              endpoint: [
                {
                  type: "websocket",
                  uri: url,
                  "content-type": "audio/l16;rate=16000",
                  headers: { from: "CALLER_NUMBER", to: "DIALLED_NUMBER", direction: "inbound" },
                },
              ],
            },
          ],
          null,
          2,
        ),
    },
    canHangup: false,
    canSendDtmf: false,
    sendsCallerNumber: false,
    audio: "16-bit linear PCM at 8, 16 or 24 kHz, set by content-type",
  },
};

export const DIALECT_ORDER: readonly StreamDialect[] = [
  "sparktg",
  "twilio",
  "plivo",
  "exotel",
  "vonage",
];

export function dialectLabel(dialect: string): string {
  return DIALECTS[dialect as StreamDialect]?.label ?? dialect;
}

/** Only a voice agent can answer a media stream — a stream carries audio, and a
    video agent's avatar has nowhere to render.

    Published is deliberately NOT required, matching the API: wiring the
    integration up before the agent is finished is a reasonable order to work in,
    and it is what `readiness: needs_agent` exists to say. Filtering unpublished
    agents out here would have made that state unreachable from the product that
    reports it. */
export function isVoiceAgent(agent: AgentResponse): boolean {
  return agent.config?.channel === "voice";
}

export function isPublished(agent: AgentResponse): boolean {
  return agent.published_version != null && agent.published_version > 0;
}

export function agentLabel(agent: AgentResponse): string {
  const name = agent.config?.name || agent.id.slice(0, 8);
  return isPublished(agent) ? name : `${name} (unpublished)`;
}
