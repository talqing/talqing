import type { Metadata } from "next";
import type { ReactNode } from "react";

import { AgentCluster } from "./agent-cluster";
import {
  ArrowRightIcon,
  Footer,
  LEGAL_ENTITY,
  loginHref,
  MarketingShell,
  SAGE,
  SiteHeader,
  SUPPORT_EMAIL,
} from "./marketing-chrome";
import { DEMO_URL, SITE_URL } from "./site";
import { LocalPrice } from "./visitor-country";

export const metadata: Metadata = {
  title: "Talqing — No-code AI voice, video & text agent builder",
  description:
    "Build, test and deploy AI voice, video and text agents without writing code. Bring your own model keys, connect a phone number or WhatsApp and publish in minutes.",
  alternates: { canonical: "/" },
  robots: { index: true, follow: true },
};

// What Google reads to name the site in results and to tie the brand to its
// logo. Only facts that are also in the footer.
const structuredData = {
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Organization",
      "@id": `${SITE_URL}/#organization`,
      name: "Talqing",
      legalName: LEGAL_ENTITY.name,
      url: SITE_URL,
      logo: `${SITE_URL}/brand/icon-192.png`,
      email: SUPPORT_EMAIL,
    },
    {
      "@type": "WebSite",
      "@id": `${SITE_URL}/#website`,
      name: "Talqing",
      url: SITE_URL,
      publisher: { "@id": `${SITE_URL}/#organization` },
    },
  ],
};

/* -------------------------------------------------------------------------
   Design notes for whoever edits this next.

   One long white sheet. Colour is spent only inside the agent cluster (see
   app/agent-cluster.tsx) and on the sage "published" chip in the editor panel.
   If you are about to add some, put it in an illustration instead.

   The signature is the agent surface: every section that shows the product
   shows a real fragment of it — a model stack, a tool list, a real Anam avatar
   — never a stock icon standing in for one. Keep the fragments truthful; the
   model ids below are the ones in backend/catalog.yaml.

   The hero is tuned so the value-prop band under it finishes inside the first
   screen on a laptop (~840px of viewport): the whole opening argument — what
   this is, what it looks like, and the three reasons — arrives without a
   scroll. If you grow the hero type or its padding, take the height back out
   of the band, or that promise quietly breaks.
   ------------------------------------------------------------------------- */

/* ---------------------------------- atoms -------------------------------- */

// Every band on the page shares this gutter. The header runs wider on purpose;
// see landing-header-shell.
function Container({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`mx-auto w-full max-w-[1800px] px-6 sm:px-10 lg:px-16 xl:px-20 2xl:px-[104px] ${className}`}
    >
      {children}
    </div>
  );
}

function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <p className="text-[13px] font-semibold uppercase tracking-[0.09em] text-[#3a3a3c]">
      {children}
    </p>
  );
}

function SectionHeading({ children }: { children: ReactNode }) {
  return (
    <h2 className="mt-6 font-display text-[clamp(2rem,3.4vw,3.25rem)] font-normal leading-[1.1] tracking-[-0.014em] text-foreground">
      {children}
    </h2>
  );
}

function PrimaryButton({
  href,
  children,
  inverted = false,
}: {
  href: string;
  children: ReactNode;
  inverted?: boolean;
}) {
  return (
    <a
      href={href}
      className={`group inline-flex h-[58px] items-center gap-3 rounded-lg px-[26px] text-[17px] font-medium transition-colors ${
        inverted
          ? "bg-background text-foreground hover:bg-background/90"
          : "bg-primary text-primary-foreground hover:bg-[#2f2f31]"
      }`}
    >
      {children}
      <ArrowRightIcon className="h-[18px] w-[18px] transition-transform duration-300 ease-out group-hover:translate-x-1" />
    </a>
  );
}

// Every text link on this page goes to the demo scheduler, which is another
// site and so opens in a new tab.
function DemoLink({
  children,
  inverted = false,
}: {
  children: ReactNode;
  inverted?: boolean;
}) {
  return (
    <a
      href={DEMO_URL}
      target="_blank"
      rel="noopener noreferrer"
      className={`text-[17px] font-medium underline underline-offset-[6px] transition-colors ${
        inverted
          ? "text-background/75 decoration-background/35 hover:text-background hover:decoration-background"
          : "text-foreground decoration-[#c9c9c9] hover:decoration-foreground"
      }`}
    >
      {children}
    </a>
  );
}

function Tick() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="mt-[5px] h-3.5 w-3.5 shrink-0 text-foreground/35"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

/* ---------------------------------- hero --------------------------------- */

function HeroSection() {
  return (
    <section className="pt-[124px] lg:pt-[142px]">
      <Container>
        <div className="grid items-center gap-12 pb-14 lg:grid-cols-[minmax(0,1.09fr)_minmax(0,1fr)] lg:gap-6 lg:pb-[60px]">
          <div className="lg:pr-6">
            <h1
              className="rise font-display text-[clamp(2.15rem,3.5vw,3.85rem)] font-normal leading-[1.12] tracking-[-0.018em] text-foreground"
              style={{ animationDelay: "0.05s" }}
            >
              Build &amp; deploy
              <br className="hidden sm:block" /> voice, video &amp; text agents
              <br className="hidden sm:block" /> in minutes, not days
            </h1>

            <p
              className="rise mt-7 max-w-[35rem] text-[18px] leading-[1.55] text-muted-foreground xl:text-[19px] 2xl:text-[20px]"
              style={{ animationDelay: "0.13s" }}
            >
              Let us worry about engineering, reliability &amp; scalability, so
              that you can focus on building quality AI agents.
            </p>

            <div
              className="rise mt-8 flex flex-wrap items-center gap-x-10 gap-y-4"
              style={{ animationDelay: "0.21s" }}
            >
              <PrimaryButton href={loginHref}>
                Start building for free
              </PrimaryButton>
              <DemoLink>Book a demo</DemoLink>
            </div>
          </div>

          {/* The top of this ramp is held down on purpose: the value-prop band
              has to finish inside the first screen, and the cluster is the one
              element on the page tall enough to push it out on its own. */}
          <div className="rise" style={{ animationDelay: "0.27s" }}>
            <AgentCluster className="mx-auto [--cluster-scale:0.42] sm:[--cluster-scale:0.685] lg:[--cluster-scale:0.55] min-[1152px]:[--cluster-scale:0.63] xl:[--cluster-scale:0.7] min-[1440px]:[--cluster-scale:0.77] 2xl:[--cluster-scale:0.8] min-[1700px]:[--cluster-scale:0.85]" />
          </div>
        </div>
      </Container>
    </section>
  );
}

/* ------------------------------- value props ----------------------------- */

const valueProps = [
  {
    title: "No more worrying about\ncode and infrastructure",
    body: "Describe the agent and publish it. We compile that definition and run the realtime session behind it.",
    icon: (
      <>
        <path d="m8 6-5 6 5 6" />
        <path d="m16 6 5 6-5 6" />
        <path d="M13.5 4l-3 16" />
      </>
    ),
  },
  {
    title: "Bring your own keys",
    body: "Your provider accounts bill you directly, at your rates. Our platform fee stays small enough never to eat your gross margin.",
    icon: (
      <>
        <circle cx="8" cy="12" r="3.6" />
        <path d="M11.6 12H21" />
        <path d="M17.8 12v3.8" />
      </>
    ),
  },
  {
    title: "Your team focuses on\nwhat matters",
    body: "Prompts, tools, MCP servers, triggers and models — the decisions that actually decide whether an agent is any good.",
    icon: (
      <>
        <path d="M4 6h8" />
        <path d="M16 6h4" />
        <circle cx="14" cy="6" r="2" />
        <path d="M4 12h4" />
        <path d="M12 12h8" />
        <circle cx="10" cy="12" r="2" />
        <path d="M4 18h8" />
        <path d="M16 18h4" />
        <circle cx="14" cy="18" r="2" />
      </>
    ),
  },
];

function ValuePropsSection() {
  return (
    <section className="border-y border-border">
      <Container>
        <div className="grid divide-y divide-border md:grid-cols-3 md:divide-x md:divide-y-0">
          {valueProps.map((prop) => (
            <div
              key={prop.title}
              className="flex flex-col gap-5 py-8 md:gap-6 md:px-6 md:py-11 md:first:pl-0 md:last:pr-0 lg:px-8 xl:flex-row xl:gap-7 xl:px-9 2xl:gap-9"
            >
              <span className="flex h-[52px] w-[52px] shrink-0 items-center justify-center rounded-full bg-secondary xl:h-[58px] xl:w-[58px] 2xl:h-[68px] 2xl:w-[68px]">
                <svg
                  viewBox="0 0 24 24"
                  className="h-[22px] w-[22px] text-[#2b2b2d] 2xl:h-6 2xl:w-6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden
                >
                  {prop.icon}
                </svg>
              </span>
              <div>
                {/* Two lines are reserved whether or not the title fills them,
                    so the three body paragraphs start on one baseline. */}
                <h2 className="min-h-[2.9em] whitespace-pre-line text-[18px] font-medium leading-[1.45] text-foreground 2xl:text-[19px]">
                  {prop.title}
                </h2>
                <p className="mt-3 text-[15.5px] leading-[1.7] text-muted-foreground 2xl:text-[16px]">
                  {prop.body}
                </p>
              </div>
            </div>
          ))}
        </div>
      </Container>
    </section>
  );
}

/* ------------------------------- provider row ---------------------------- */

// Marks and labels lifted from backend/catalog.yaml and
// services/telephony/catalog.py — the same source the editor dropdowns read.
// The files are copied into public/logos rather than hot-linked: a marketing
// page should not make fourteen third-party requests to render one strip, and
// a provider rotating its favicon should not silently break our fold.
//
// These are here because we support them, not because they are customers.
const providers = [
  ["OpenAI", "openai.svg"],
  ["Gemini", "gemini.png"],
  ["xAI", "xai.png"],
  ["OpenRouter", "openrouter.png"],
  ["Deepgram", "deepgram.png"],
  ["ElevenLabs", "elevenlabs.png"],
  ["Soniox", "soniox.png"],
  ["Sarvam", "sarvam.svg"],
  ["Raya", "raya.png"],
  ["Anam", "anam.png"],
  ["ai-coustics", "aicoustics.png"],
  ["Twilio", "twilio.png"],
  ["Plivo", "plivo.png"],
  ["Exotel", "exotel.png"],
  ["Vobiz", "vobiz.png"],
];

const edgeFade =
  "linear-gradient(to right, transparent, #000 4%, #000 92%, transparent)";

function ProviderMarks({ hidden }: { hidden?: boolean }) {
  return (
    <>
      {providers.map(([name, file]) => (
        <span
          key={name}
          className="mr-12 flex shrink-0 items-center gap-3"
          aria-hidden={hidden}
        >
          <img
            src={`/logos/${file}`}
            alt=""
            width={28}
            height={28}
            loading="lazy"
            decoding="async"
            className="h-7 w-7 shrink-0 object-contain"
          />
          <span className="whitespace-nowrap text-[17px] font-medium tracking-[-0.01em] text-[#3f3f43]">
            {name}
          </span>
        </span>
      ))}
    </>
  );
}

function ProviderSection() {
  return (
    <section className="border-b border-border py-10 lg:py-12">
      <Container>
        <div className="flex flex-col gap-7 lg:flex-row lg:items-center lg:gap-14">
          {/* Not "bring your own keys for" — the value prop directly above this
              band already says exactly that, and the two stack close enough to
              read as a copy-paste slip. */}
          <p className="shrink-0 text-[13px] font-semibold uppercase tracking-[0.08em] text-foreground">
            Your own keys for
          </p>
          {/* The track holds the list twice so translateX(-50%) loops with no
              seam; the second pass is decorative and hidden from readers. */}
          <div
            className="relative min-w-0 flex-1 overflow-hidden"
            style={{ maskImage: edgeFade, WebkitMaskImage: edgeFade }}
          >
            <div className="marquee-track flex w-max items-center">
              <ProviderMarks />
              <ProviderMarks hidden />
            </div>
          </div>
        </div>
      </Container>
    </section>
  );
}

/* -------------------------------- channels ------------------------------- */

// Ordered the way a reader meets them, cheapest and simplest first: text is
// free and needs nothing but a prompt, voice adds a speech stack and a carrier,
// video adds a face on top of voice. The pricing band lower down repeats this
// order, and the two must not disagree.
const channels = [
  {
    name: "Text",
    body: "Chat on WhatsApp, on Telegram, in a widget embedded in your app or straight over the API, with the same tools behind it.",
    points: [
      "WhatsApp numbers and Telegram bots",
      "Streamed replies over SSE",
      "Images in, grounded answers out",
    ],
    icon: (
      <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-5.2A8 8 0 1 1 21 12Z" />
    ),
  },
  {
    name: "Voice",
    body: "A browser call, a real phone number or a WhatsApp call, running either a speech pipeline you assemble yourself or a single realtime model.",
    points: [
      "STT + LLM + TTS, or one realtime model",
      "Inbound and outbound PSTN on your carrier",
      "Recording and post-call analysis",
    ],
    icon: (
      <>
        <path d="M4 12v-2" />
        <path d="M8 17V7" />
        <path d="M12 20V4" />
        <path d="M16 17V7" />
        <path d="M20 12v-2" />
      </>
    ),
  },
  {
    name: "Video",
    body: "The same voice agent wearing an Anam avatar, so the person on the other end has a face to talk to.",
    points: [
      "Anam avatars, picked per agent",
      "Identical prompt and tools",
      "Browser sessions, no plugin",
    ],
    icon: (
      <>
        <path d="M16.5 10.4 20.6 8.2a.75.75 0 0 1 1.1.66v6.28a.75.75 0 0 1-1.1.66l-4.1-2.2" />
        <rect x="2.4" y="6" width="14.1" height="12" rx="2.6" />
      </>
    ),
  },
];

function ChannelsSection() {
  return (
    <section
      id="channels"
      className="scroll-mt-24 border-y border-border bg-secondary py-24 lg:py-32"
    >
      <Container>
        <div className="max-w-[40rem]">
          <Eyebrow>Channels</Eyebrow>
          <SectionHeading>One definition. Three ways to talk.</SectionHeading>
          <p className="mt-7 text-[17px] leading-[1.7] text-muted-foreground">
            The prompt and the tools do not change when the channel
            does. Switching it changes which model slots the agent
            fills, not how it behaves.
          </p>
        </div>

        <div className="mt-14 grid gap-px overflow-hidden rounded-[14px] border border-border bg-border md:grid-cols-3">
          {channels.map((channel) => (
            <div
              key={channel.name}
              className="flex flex-col bg-background p-8 lg:p-10"
            >
              <svg
                viewBox="0 0 24 24"
                className="h-6 w-6 text-[#2b2b2d]"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden
              >
                {channel.icon}
              </svg>
              <h3 className="mt-6 font-display text-[28px] leading-none tracking-[-0.01em] text-foreground">
                {channel.name}
              </h3>
              <p className="mt-4 flex-1 text-[15.5px] leading-[1.7] text-muted-foreground">
                {channel.body}
              </p>
              <ul className="mt-7 space-y-3 border-t border-border pt-6">
                {channel.points.map((point) => (
                  <li
                    key={point}
                    className="flex gap-2.5 text-[14.5px] leading-[1.5] text-foreground/80"
                  >
                    <Tick />
                    {point}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </Container>
    </section>
  );
}

/* ------------------------------ capabilities ----------------------------- */

function PanelLabel({ children }: { children: ReactNode }) {
  return (
    <p className="text-[11px] font-semibold uppercase tracking-[0.13em] text-muted-foreground">
      {children}
    </p>
  );
}

function StackChip({ role, value }: { role: string; value: string }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-[7px] border border-border px-2.5 py-[7px]">
      <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        {role}
      </span>
      <span className="text-[12.5px] text-foreground/85">{value}</span>
    </span>
  );
}

const panelTools = [
  ["look_up_order", "HTTP"],
  ["issue_refund", "Code"],
  ["transfer_to_human", "Transfer"],
];

// Real providers from the integrations catalog, with their own marks. Logos
// are self-hosted in /public/logos, like the provider strip's.
const panelMcps = [
  ["Calendly", "calendly.png"],
  ["HubSpot", "hubspot.png"],
];

// A slice of the agent editor, not a screenshot: it stays legible at any width
// and it cannot go stale the way an exported PNG does. The model ids are real.
function AgentEditorPanel() {
  return (
    <div className="overflow-hidden rounded-[14px] border border-border bg-background shadow-[0_40px_84px_-54px_rgba(17,17,17,0.45)]">
      <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-4">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="truncate text-[14px] font-medium text-foreground">
            Refund desk
          </span>
          <span className="rounded-full bg-secondary px-2 py-[3px] text-[11px] font-medium text-muted-foreground">
            voice
          </span>
        </div>
        <span
          className="inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium"
          style={{
            color: "#3e6b3b",
            backgroundColor: "rgba(91,125,88,0.1)",
            boxShadow: "inset 0 0 0 1px rgba(91,125,88,0.22)",
          }}
        >
          <span
            className="h-[5px] w-[5px] rounded-full"
            style={{ backgroundColor: SAGE }}
          />
          v7 · published
        </span>
      </div>

      <div className="border-b border-border px-5 py-5">
        <PanelLabel>Model stack</PanelLabel>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <StackChip role="stt" value="deepgram / nova-3" />
          <StackChip role="llm" value="openai / gpt-5.6-sol" />
          <StackChip role="tts" value="elevenlabs / turbo v2.5" />
        </div>
      </div>

      <div className="border-b border-border px-5 py-5">
        <PanelLabel>System prompt</PanelLabel>
        <p className="mt-3 text-[13.5px] leading-[1.75] text-foreground/75">
          You handle refund requests for Northwind. Confirm the order number
          before you look anything up, and never promise a refund the policy
          does not allow. You are speaking with{" "}
          <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[12px] text-foreground/90">
            {"{{userdata.name}}"}
          </span>
          .
        </p>
      </div>

      <div className="border-b border-border px-5 py-5">
        <PanelLabel>Tools</PanelLabel>
        <ul className="mt-1.5 divide-y divide-border">
          {panelTools.map(([name, kind]) => (
            <li
              key={name}
              className="flex items-center justify-between gap-3 py-[11px]"
            >
              <span className="truncate font-mono text-[12.5px] text-foreground/85">
                {name}
              </span>
              <span className="shrink-0 rounded-[6px] bg-secondary px-2 py-[3px] text-[11px] text-muted-foreground">
                {kind}
              </span>
            </li>
          ))}
        </ul>
      </div>

      <div className="grid gap-5 px-5 py-5 sm:grid-cols-2">
        <div className="min-w-0">
          <PanelLabel>MCP servers</PanelLabel>
          <ul className="mt-3 flex flex-wrap gap-2">
            {panelMcps.map(([name, file]) => (
              <li
                key={name}
                className="flex items-center gap-2 rounded-[8px] border border-border px-2.5 py-1.5 text-[12.5px] text-foreground/85"
              >
                <img
                  src={`/logos/${file}`}
                  alt=""
                  width={16}
                  height={16}
                  loading="lazy"
                  decoding="async"
                  className="h-4 w-4 shrink-0 rounded-[3px] object-contain"
                />
                {name}
              </li>
            ))}
          </ul>
        </div>
        <div className="min-w-0">
          <PanelLabel>FAQs</PanelLabel>
          <div className="mt-3 flex items-center justify-between gap-3 py-1.5">
            <span className="truncate text-[13px] text-foreground/85">Returns and refunds</span>
            <span className="shrink-0 rounded-[6px] bg-secondary px-2 py-[3px] text-[11px] text-muted-foreground">
              14 questions
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

const capabilities = [
  {
    title: "Tools that do real work",
    body: "HTTP requests, code steps, conditions, generated replies, frontend actions, MCP operations, handoffs and end-call — composed as a tree, not a script.",
    icon: (
      <path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L4 16.8 7.2 20l5.3-5.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.1-.5-.5-2.1 2.2-2.1Z" />
    ),
  },
  {
    title: "Phone numbers, your carrier",
    body: "Connect Twilio, Plivo, Exotel or Vobiz, import your DIDs, and point inbound calls at a published agent. PSTN billing stays with the carrier.",
    icon: (
      <path d="M6.5 3h2.2l1.6 4-2 1.4a12 12 0 0 0 5.3 5.3l1.4-2 4 1.6v2.2a2.5 2.5 0 0 1-2.7 2.5A16.5 16.5 0 0 1 4 6.7 2.5 2.5 0 0 1 6.5 3Z" />
    ),
  },
  {
    title: "Copilot in every editor",
    body: "An AI builder that can rewrite the prompt, swap models, add tools and publish — and shows you exactly what it changed.",
    icon: (
      <>
        <path d="M12 3.5 13.9 9 19.5 11 13.9 13 12 18.5 10.1 13 4.5 11 10.1 9 12 3.5Z" />
        <path d="M18.5 16.5 19.3 18.7 21.5 19.5 19.3 20.3 18.5 22.5 17.7 20.3 15.5 19.5 17.7 18.7 18.5 16.5Z" />
      </>
    ),
  },
  {
    title: "Answers in your words",
    body: "Write the questions people ask and the answers you want given. The agent looks them up mid-conversation and says them the same way every time.",
    icon: (
      <>
        <path d="M4.5 18.5V7A2.5 2.5 0 0 1 7 4.5h10A2.5 2.5 0 0 1 19.5 7v7a2.5 2.5 0 0 1-2.5 2.5H9l-4.5 3Z" />
        <path d="M9.8 9.2a2.3 2.3 0 1 1 3.3 2.1c-.7.4-1.1.8-1.1 1.5M12 14.6v.1" />
      </>
    ),
  },
  {
    title: "Sessions you can audit",
    body: "Transcript, tool invocations, final userdata, per-provider usage and a cost breakdown for every session — test runs included. Signed webhooks push the same events to your own systems.",
    icon: (
      <>
        <path d="M4 4v16h16" />
        <path d="m8 14 3-3 3 2 4.5-5.5" />
      </>
    ),
  },
  {
    title: "API and MCP, not just a UI",
    body: "Every dashboard action is an API operation, and the same set is exposed over MCP — so Claude Code or Codex can build the agent with you.",
    icon: (
      <>
        <rect x="3" y="4" width="18" height="6.5" rx="2" />
        <rect x="3" y="13.5" width="18" height="6.5" rx="2" />
        <path d="M7 7.25h.01" />
        <path d="M7 16.75h.01" />
      </>
    ),
  },
];

function CapabilitiesSection() {
  return (
    <section id="capabilities" className="scroll-mt-24 py-24 lg:py-32">
      <Container>
        <div className="max-w-[42rem]">
          <Eyebrow>Capabilities</Eyebrow>
          <SectionHeading>
            Everything you would have written by hand.
          </SectionHeading>
          <p className="mt-7 text-[17px] leading-[1.7] text-muted-foreground">
            Build it yourself in the dashboard, hand it to Copilot and review
            what changed, or drive the whole thing from Claude Code over MCP.
            Same agent either way.
          </p>
        </div>

        {/* The panel is the anchor: six blocks of text with icons would read as
            a feature wall, and this way the reader can see the thing the six
            bullets are describing while they read them. An even count, so the
            two columns end level. */}
        <div className="mt-16 grid gap-14 lg:grid-cols-[minmax(0,0.78fr)_minmax(0,1fr)] lg:gap-16 xl:gap-20">
          <div>
            <div className="lg:sticky lg:top-28">
              <AgentEditorPanel />
            </div>
          </div>

          <div className="grid gap-x-12 gap-y-12 sm:grid-cols-2 lg:gap-y-14">
            {capabilities.map((capability) => (
              <div key={capability.title}>
                <svg
                  viewBox="0 0 24 24"
                  className="h-[23px] w-[23px] text-[#2b2b2d]"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.55"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden
                >
                  {capability.icon}
                </svg>
                <h3 className="mt-5 text-[18px] font-medium tracking-[-0.01em] text-foreground">
                  {capability.title}
                </h3>
                <p className="mt-3 text-[15.5px] leading-[1.7] text-muted-foreground">
                  {capability.body}
                </p>
              </div>
            ))}
          </div>
        </div>
      </Container>
    </section>
  );
}

/* -------------------------------- platform fee --------------------------- */

// The only money Talqing takes. Keep these numbers and the ones in
// backend/catalog.yaml (`platform_fee_per_minute`, `platform_fee_per_message`,
// and `inr_per_usd` for the ₹ column) telling the same story — a price a visitor read here and a price the
// meter charged are the one pair of numbers on this page that a customer will
// actually check. Which column a visitor sees is decided in
// app/visitor-country.tsx.
type FeeQuote = { amount: string; unit: string | null };

const platformFee: {
  channel: string;
  inr: FeeQuote;
  usd: FeeQuote;
  body: string;
}[] = [
  {
    channel: "Text",
    inr: { amount: "₹0.01", unit: "per message" },
    usd: { amount: "$0.0001", unit: "per message" },
    body: "Charged for each message your agent answers, on the API, WhatsApp or Telegram. A chat left open costs nothing while nobody is writing.",
  },
  {
    channel: "Voice",
    inr: { amount: "₹0.35", unit: "per minute" },
    usd: { amount: "$0.0035", unit: "per minute" },
    body: "Browser, phone and WhatsApp calls alike. Your carrier bills the PSTN leg; we bill the agent that ran on it.",
  },
  {
    channel: "Video",
    inr: { amount: "₹1", unit: "per minute" },
    usd: { amount: "$0.01", unit: "per minute" },
    body: "The same voice agent with an Anam avatar attached, billed for the minutes the avatar is on screen.",
  },
];

function FeeFigure({ amount, unit }: FeeQuote) {
  return (
    <>
      <span className="font-display text-[52px] leading-none tracking-[-0.02em] text-foreground md:text-[44px] lg:text-[52px]">
        {amount}
      </span>
      {unit && (
        <span className="whitespace-nowrap text-[15px] text-muted-foreground">
          {unit}
        </span>
      )}
    </>
  );
}

function PricingSection() {
  return (
    <section
      id="pricing"
      className="scroll-mt-24 border-y border-border bg-secondary py-24 lg:py-32"
    >
      <Container>
        <div className="max-w-[42rem]">
          <Eyebrow>Platform fee</Eyebrow>
          <SectionHeading>One flat fee. Nothing else.</SectionHeading>
          <p className="mt-7 text-[17px] leading-[1.7] text-muted-foreground">
            Talqing is strict bring-your-own-keys, so model, speech, avatar and
            carrier costs go straight to your own accounts at your own rates. A
            platform fee — per minute of a call, per message of a chat — is the
            only thing we add on top, and the only thing we ever charge you for.
          </p>
        </div>

        {/* One subgrid row per line of a card, so the three dividers stay level
            when a unit wraps under its figure in one card and not the next. */}
        <div className="mt-14 grid gap-px overflow-hidden rounded-[14px] border border-border bg-border md:grid-cols-3 md:grid-rows-[auto_auto_1fr]">
          {platformFee.map((tier) => (
            <div
              key={tier.channel}
              className="flex flex-col bg-background p-8 md:row-span-3 md:grid md:grid-rows-subgrid md:gap-y-0 lg:p-10"
            >
              <p className="text-[13px] font-semibold uppercase tracking-[0.09em] text-[#3a3a3c]">
                {tier.channel}
              </p>
              {/* Between md and xl a third of the container is too narrow for
                  "$0.0035" and its unit on one line, so every unit sits beneath
                  its figure there — all three cards at once, rather than
                  whichever ones happen to wrap. */}
              <p className="mt-6 flex flex-wrap items-baseline gap-x-2.5 gap-y-1 md:flex-col md:items-start xl:flex-row xl:items-baseline">
                <LocalPrice
                  inr={<FeeFigure {...tier.inr} />}
                  usd={<FeeFigure {...tier.usd} />}
                />
              </p>
              <p className="mt-6 border-t border-border pt-6 text-[15.5px] leading-[1.7] text-muted-foreground">
                {tier.body}
              </p>
            </div>
          ))}
        </div>

        <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[15.5px] leading-[1.7] text-muted-foreground">
            Discounts are available at committed volume on both voice and video.
          </p>
          <DemoLink>Talk to us about volume</DemoLink>
        </div>
      </Container>
    </section>
  );
}

/* ----------------------------------- faq --------------------------------- */

const faqs = [
  {
    question: "What can I build with Talqing?",
    answer:
      "Voice, video and text agents that talk to your users — on a phone number, in the browser, on WhatsApp or Telegram — call tools, fire webhooks, capture session state and hand off to other agents. Realtime voice is the centre of gravity; the same agent model also runs unattended tasks that take typed inputs and return a typed result.",
  },
  {
    question: "Do I need to write LiveKit Agents code?",
    answer:
      "No. You author the agent in the dashboard, through Copilot, or over MCP from your own editor. Talqing compiles that definition into a LiveKit AgentSession and runs it on the platform runtime.",
  },
  {
    question: "Whose API keys does it use?",
    answer:
      "Yours. Talqing is strict bring-your-own-keys: you connect your own OpenAI, Gemini, xAI, OpenRouter, Deepgram, ElevenLabs, Soniox, Sarvam, Raya and Anam accounts, and those providers bill you directly. We never resell tokens or minutes — the platform fee is the whole of what we charge.",
  },
  {
    question: "Can I test an agent before publishing?",
    answer:
      "Yes. Run browser voice, video and text sessions from the dashboard, then read the transcript, tool calls, captured fields, provider usage and cost for that session before you publish anything.",
  },
  {
    question: "How do tools work without code?",
    answer:
      "A tool is an operation tree. You compose HTTP requests, conditional branches, code steps, generated replies, frontend actions, MCP and provider operations, handoffs, end-call and webhook triggers from the editor — and there is a code operation for the cases that genuinely need one.",
  },
  {
    question: "Is telephony included?",
    answer:
      "Yes, on your own carrier account — Twilio, Plivo, Exotel or Vobiz. Connect the account under Phone Numbers, import your DIDs, assign a published voice agent for inbound, and place outbound calls one at a time or as a scheduled batch, from the dashboard or the API. If a contact-centre platform already holds your numbers, it can stream call audio to the agent over a WebSocket instead. PSTN charges stay with your carrier; Talqing runs the agent.",
  },
];

function FaqSection() {
  return (
    <section
      id="faq"
      className="scroll-mt-24 border-t border-border py-24 lg:py-32"
    >
      <Container>
        <div className="grid gap-12 lg:grid-cols-[minmax(0,0.7fr)_minmax(0,1fr)] lg:gap-20">
          <div>
            <div className="lg:sticky lg:top-32">
              <Eyebrow>FAQ</Eyebrow>
              <SectionHeading>
                Questions we get
                <br className="hidden sm:block" /> before the first call.
              </SectionHeading>
              <p className="mt-7 max-w-sm text-[16px] leading-[1.7] text-muted-foreground">
                Something not covered here?{" "}
                <a
                  href={`mailto:${SUPPORT_EMAIL}`}
                  className="text-foreground underline decoration-[#c9c9c9] underline-offset-4 transition-colors hover:decoration-foreground"
                >
                  Write to us
                </a>{" "}
                — a person answers.
              </p>
            </div>
          </div>

          <div className="border-t border-border">
            {faqs.map((faq, index) => (
              <details
                key={faq.question}
                open={index === 0}
                className="group border-b border-border"
              >
                <summary className="flex cursor-pointer list-none items-start justify-between gap-6 py-6 [&::-webkit-details-marker]:hidden">
                  <span className="text-[17px] font-medium leading-snug text-foreground lg:text-[18px]">
                    {faq.question}
                  </span>
                  <span className="mt-[3px] flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border text-muted-foreground transition-colors group-hover:border-foreground/30 group-hover:text-foreground">
                    <svg
                      viewBox="0 0 24 24"
                      className="h-3.5 w-3.5"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      aria-hidden
                    >
                      <path d="M5 12h14" />
                      <path
                        d="M12 5v14"
                        className="origin-center transition-transform duration-200 group-open:scale-y-0"
                      />
                    </svg>
                  </span>
                </summary>
                <p className="max-w-[48rem] pb-7 text-[15.5px] leading-[1.75] text-muted-foreground lg:pr-10">
                  {faq.answer}
                </p>
              </details>
            ))}
          </div>
        </div>
      </Container>
    </section>
  );
}

/* --------------------------------- closing ------------------------------- */

function ClosingSection() {
  return (
    <section className="bg-foreground py-24 text-background lg:py-32">
      <Container>
        <div className="flex flex-col gap-12 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h2 className="font-display text-[clamp(2.2rem,4vw,3.75rem)] font-normal leading-[1.08] tracking-[-0.016em]">
              Your first agent
              <br className="hidden sm:block" /> is one prompt away.
            </h2>
            <p className="mt-7 max-w-[30rem] text-[17px] leading-[1.7] text-background/60">
              Sign in with Google, tell it what the job is, and place a test
              call before your coffee goes cold.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-x-10 gap-y-4 lg:pb-2">
            <PrimaryButton href={loginHref} inverted>
              Start building
            </PrimaryButton>
            <DemoLink inverted>Book a demo</DemoLink>
          </div>
        </div>
      </Container>
    </section>
  );
}

export default function Landing() {
  return (
    <MarketingShell>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(structuredData) }}
      />
      <SiteHeader />
      <HeroSection />
      <ValuePropsSection />
      <ProviderSection />
      <ChannelsSection />
      <CapabilitiesSection />
      <PricingSection />
      <FaqSection />
      <ClosingSection />
      <Footer />
    </MarketingShell>
  );
}
