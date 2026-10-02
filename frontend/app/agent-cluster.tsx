import type { CSSProperties, ReactNode } from "react";

import { SAGE } from "./marketing-chrome";

/* -------------------------------------------------------------------------
   The three-card agent cluster — a voice call, a video call with a real Anam
   avatar, and a chat thread. It is the product's opening statement and it
   appears in two places (the landing hero and the /login panel), so it lives
   here rather than being copied: the two must never drift apart.

   It is ONE composition, not three cards in a row. It scales rather than
   reflows — three cards stacked on a phone would read as three unrelated boxes
   instead of "one agent, three ways to talk". The art below is therefore drawn
   at a fixed 760×362 and the `.agent-cluster` wrapper in globals.css scales it.

   The geometry is symmetric and every number in it is load-bearing:

     video   left 232, top 0,  296 × 362   ← centred: (760 − 296) / 2
     voice   left 0,   top 84, 240 × 278   ← 84 + 278 = 362, flush at the base
     text    left 520, top 84, 240 × 278   ← 760 − 240, mirrors voice exactly

   The two side cards are the SAME size and sit on the SAME baseline as the
   video card; each is tucked 8px under it. They used to be 206×278 and 256×304
   with the text card hanging 26px lower, which just read as a mistake.

   Their heights are explicit rather than content-driven, so the composition
   cannot be resized by an edit to the copy inside it — but that cuts both ways:
   there is ~4px of slack in each, so a longer message will overflow instead of
   growing the card. Both messages have to stay at two lines.

   Colour is spent in exactly three places in here — the sage live dot, the
   sage waveforms, and the red hang-up control. The avatar still is the fourth,
   and it is a photograph, so it is allowed. Do not add a fifth.
   ------------------------------------------------------------------------- */

const WAVE = "#4f6b4d"; // speech, at rest
const HANGUP = "#df5248";

// Hand-authored rather than generated. Real speech is busy in the middle and
// trails off; a sine envelope produces an even picket fence that reads as
// decoration, and a generated one is no more "correct" than a drawn one here.
const VOICE_STRIP = [
  0.24, 0.5, 0.76, 0.44, 0.9, 0.6, 1, 0.54, 0.8, 0.34, 0.7, 0.92, 0.5, 0.76,
  0.4, 0.86, 0.54, 0.66, 0.3, 0.5, 0.24, 0.4, 0.17, 0.28, 0.12, 0.17, 0.1, 0.1,
];

// The level meter inside the video frame: mostly quiet, with a few peaks.
const FRAME_STRIP = [
  0.04, 0.04, 0.12, 0.04, 0.3, 0.04, 0.16, 0.55, 0.24, 0.95, 1, 0.64, 0.85,
  0.28, 0.5, 0.12, 0.3, 0.05, 0.04,
];

const LISTENING_GLYPH = [0.34, 0.6, 0.86, 1, 0.8, 0.55, 0.3];

export function LiveDot({ size = 8 }: { size?: number }) {
  return (
    <span
      className="relative flex shrink-0"
      style={{ width: size, height: size }}
    >
      <span
        className="live-halo absolute inset-0 rounded-full"
        style={{ backgroundColor: SAGE }}
      />
      <span
        className="relative rounded-full"
        style={{ width: size, height: size, backgroundColor: SAGE }}
      />
    </span>
  );
}

function Bars({
  heights,
  height,
  barWidth,
  gap,
  color,
  className = "",
}: {
  heights: number[];
  height: number;
  barWidth: number;
  gap: number;
  color: string;
  className?: string;
}) {
  return (
    <div
      className={`flex items-center ${className}`}
      style={{ height, gap }}
      aria-hidden
    >
      {heights.map((value, index) => (
        <span
          key={index}
          className="wave-bar rounded-full"
          style={{
            width: barWidth,
            height: Math.max(barWidth, Math.round(value * height)),
            backgroundColor: color,
            animationDelay: `${(index % 11) * 0.09}s`,
          }}
        />
      ))}
    </div>
  );
}

function CardShell({
  children,
  style,
  className = "",
}: {
  children: ReactNode;
  style: CSSProperties;
  className?: string;
}) {
  return (
    <div
      className={`absolute rounded-[14px] border border-[#efefef] bg-background ${className}`}
      style={style}
    >
      {children}
    </div>
  );
}

export function CameraIcon({ className }: { className: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M16.5 10.4 20.6 8.2a.75.75 0 0 1 1.1.66v6.28a.75.75 0 0 1-1.1.66l-4.1-2.2" />
      <rect x="2.4" y="6" width="14.1" height="12" rx="2.6" />
    </svg>
  );
}

function VoiceCard() {
  return (
    <CardShell
      style={{
        left: 0,
        top: 84,
        width: 240,
        height: 278,
        padding: 24,
        boxShadow: "0 18px 40px -22px rgba(17,17,17,0.28)",
      }}
    >
      <div className="flex items-center gap-3.5">
        <span className="text-[15px] font-medium leading-[18px] text-[#1a1a1a]">
          Voice Agent
        </span>
        <LiveDot />
      </div>

      <Bars
        heights={LISTENING_GLYPH}
        height={36}
        barWidth={3}
        gap={3}
        color={WAVE}
        className="mt-[34px]"
      />

      <p className="mt-[18px] text-[19px] leading-[30px] text-[#111]">
        Hello. How can I help you today?
      </p>

      <Bars
        heights={VOICE_STRIP}
        height={34}
        barWidth={3}
        gap={1.6}
        color={WAVE}
        className="mb-[10px] mt-[18px]"
      />
    </CardShell>
  );
}

// Cara, one of the Anam stock avatars the video channel actually renders — the
// same catalogue the agent editor picks from. A flat drawn portrait used to sit
// here to avoid reading as a customer testimonial; the real avatar is both more
// honest about what the product does and the only thing that makes this frame
// read as a video call rather than an illustration.
//
// The file is served from public/ rather than Anam's CDN: a marketing hero
// should not block on a third-party host, and their preview URLs are not a
// contract we can rely on.
function AvatarFrame() {
  return (
    <div className="relative h-full w-full bg-[#e6e9e4]">
      <img
        src="/anam-avatar-cara.webp"
        alt="An Anam avatar agent on a video call"
        width={640}
        height={473}
        className="h-full w-full object-cover"
      />

      <span className="absolute left-[9px] top-[9px] rounded-[5px] bg-[#111]/40 px-[7px] py-[3px] text-[10px] font-medium leading-[13px] text-white">
        Cara · Anam
      </span>

      {/* the caller's audio level, sitting on the frame like a meter */}
      <div
        className="absolute inset-x-0 bottom-0 flex h-[52px] items-end justify-center pb-[11px]"
        style={{
          background:
            "linear-gradient(to top, rgba(16,16,17,0.5), rgba(16,16,17,0))",
        }}
      >
        <Bars
          heights={FRAME_STRIP}
          height={24}
          barWidth={2}
          gap={2.6}
          color="rgba(255,255,255,0.92)"
        />
      </div>
    </div>
  );
}

function CallControl({
  label,
  size,
  children,
  danger = false,
}: {
  label: string;
  size: number;
  children: ReactNode;
  danger?: boolean;
}) {
  return (
    <span
      role="img"
      aria-label={label}
      className="flex items-center justify-center rounded-full"
      style={{
        width: size,
        height: size,
        backgroundColor: danger ? HANGUP : "#e9e9e9",
        color: danger ? "#ffffff" : "#6f6f74",
      }}
    >
      {children}
    </span>
  );
}

function VideoCard() {
  return (
    <CardShell
      className="z-20"
      style={{
        left: 232,
        top: 0,
        width: 296,
        height: 362,
        padding: 20,
        boxShadow: "0 26px 56px -26px rgba(17,17,17,0.32)",
      }}
    >
      <div className="flex items-center justify-center gap-2.5 pb-[18px] pt-[6px]">
        <CameraIcon className="h-[19px] w-[19px] text-[#1a1a1a]" />
        <span className="text-[15px] font-medium leading-[18px] text-[#1a1a1a]">
          Video Agent
        </span>
        <LiveDot />
      </div>

      <div
        className="overflow-hidden rounded-[8px]"
        style={{ width: 256, height: 189 }}
      >
        <AvatarFrame />
      </div>

      <div className="flex items-center justify-center gap-[22px] pb-[10px] pt-[20px]">
        <CallControl label="Microphone" size={48}>
          <svg
            viewBox="0 0 24 24"
            className="h-[19px] w-[19px]"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
            aria-hidden
          >
            <rect x="9" y="3" width="6" height="11" rx="3" />
            <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0" />
            <path d="M12 18v3" />
          </svg>
        </CallControl>
        <CallControl label="End call" size={58} danger>
          <svg
            viewBox="0 0 24 24"
            className="h-[22px] w-[22px]"
            fill="currentColor"
            aria-hidden
          >
            <path d="M12 9.4c-2.2 0-4.3.35-6.2 1v2.5c0 .55-.33 1.04-.84 1.24l-2.05.8a1.33 1.33 0 0 1-1.72-.8l-.83-2.4a1.33 1.33 0 0 1 .41-1.47C3.2 8 7.3 6.4 12 6.4s8.8 1.6 11.23 3.87c.4.37.56.94.41 1.47l-.83 2.4a1.33 1.33 0 0 1-1.72.8l-2.05-.8a1.33 1.33 0 0 1-.84-1.24v-2.5c-1.9-.65-4-1-6.2-1Z" />
          </svg>
        </CallControl>
        <CallControl label="Camera" size={48}>
          <CameraIcon className="h-[19px] w-[19px]" />
        </CallControl>
      </div>
    </CardShell>
  );
}

function TextCard() {
  return (
    <CardShell
      style={{
        left: 520,
        top: 84,
        width: 240,
        height: 278,
        padding: 24,
        boxShadow: "0 18px 40px -22px rgba(17,17,17,0.28)",
      }}
    >
      <div className="flex items-center gap-3.5">
        <span className="text-[15px] font-medium leading-[18px] text-[#1a1a1a]">
          Text Agent
        </span>
        <LiveDot />
      </div>

      <svg
        viewBox="0 0 32 31"
        className="mt-[26px] h-[32px] w-[33px] text-[#2c2c2c]"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        aria-hidden
      >
        <path
          d="M16 2.6c7.3 0 13.4 4.5 13.4 10.2S23.3 23 16 23c-1.3 0-2.6-.15-3.8-.42l-1.75 2a1 1 0 0 1-1.75-.66V21.4C5.1 19.6 2.6 16.6 2.6 12.8 2.6 7.1 8.7 2.6 16 2.6Z"
          strokeLinejoin="round"
        />
        <circle cx="10.8" cy="12.7" r="1.35" fill="currentColor" stroke="none" />
        <circle cx="16" cy="12.7" r="1.35" fill="currentColor" stroke="none" />
        <circle cx="21.2" cy="12.7" r="1.35" fill="currentColor" stroke="none" />
      </svg>

      <p className="mt-[22px] text-[18px] leading-[28px] text-[#111]">
        Of course — let me pull that order up.
      </p>

      <div className="mt-[24px] flex h-12 items-center gap-2 rounded-[10px] border border-[#e6e6e6] px-3.5">
        {/* Kept short enough to clear the send icon at this card width — the
            `truncate` is a backstop, and a clipped placeholder reads as a bug. */}
        <span className="flex-1 truncate text-[15px] text-[#9f9fa0]">
          Type a message…
        </span>
        <svg
          viewBox="0 0 24 24"
          className="h-[19px] w-[19px] shrink-0 text-[#2c2c2c]"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinejoin="round"
          aria-hidden
        >
          <path d="M21.4 3.1 2.9 10.2l7.4 2.6 2.6 7.4Z" />
          <path d="m10.3 12.8 6.6-6.6" />
        </svg>
      </div>
    </CardShell>
  );
}

/**
 * The cluster. Size it with `--cluster-scale` on `className`, per breakpoint —
 * `"[--cluster-scale:0.52] xl:[--cluster-scale:0.7]"`. That one number sets the
 * width, the height and the transform together (see `.agent-cluster` in
 * globals.css), so never pass a width or a height of your own.
 */
export function AgentCluster({ className = "" }: { className?: string }) {
  return (
    <div className={`agent-cluster ${className}`}>
      <div className="relative">
        <VoiceCard />
        <VideoCard />
        <TextCard />
      </div>
    </div>
  );
}
