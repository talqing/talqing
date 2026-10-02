import type { Config } from "tailwindcss";

/* talqing design system — "pristine instrument".
   A near-white canvas (#fafafa) with pure-white floating surfaces, hairline
   borders, whisper-soft shadows, a single confident sans, and near-black
   primary actions. Color is reserved for semantic status; everything else is
   neutral. Tailwind-utility-only: status washes come from /opacity modifiers
   (bg-live/10, border-info/30) rather than separate tint tokens. */
const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./lib/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    screens: {
      sm: "640px",
      md: "768px",
      tablet: "900px",
      lg: "1024px",
      xl: "1280px",
      "2xl": "1536px",
    },
    extend: {
      colors: {
        background: "rgb(var(--background) / <alpha-value>)",
        foreground: "rgb(var(--foreground) / <alpha-value>)",
        card: "rgb(var(--card) / <alpha-value>)",
        "card-foreground": "rgb(var(--card-foreground) / <alpha-value>)",
        popover: "rgb(var(--popover) / <alpha-value>)",
        "popover-foreground": "rgb(var(--popover-foreground) / <alpha-value>)",
        primary: "rgb(var(--primary) / <alpha-value>)",
        "primary-foreground": "rgb(var(--primary-foreground) / <alpha-value>)",
        secondary: "rgb(var(--secondary) / <alpha-value>)",
        "secondary-foreground": "rgb(var(--secondary-foreground) / <alpha-value>)",
        "muted-foreground": "rgb(var(--muted-foreground) / <alpha-value>)",
        accent: "rgb(var(--accent) / <alpha-value>)",
        "accent-foreground": "rgb(var(--accent-foreground) / <alpha-value>)",
        border: "rgb(var(--border) / <alpha-value>)",
        input: "rgb(var(--input) / <alpha-value>)",
        ring: "rgb(var(--ring) / <alpha-value>)",
        /* Surfaces, back to front. */
        canvas: "#fafafa", // app backdrop (page + sidebar) — whisper gray, reads white
        surface: "#ffffff", // cards / panels / raised inputs / popups
        subtle: "#f4f4f5", // recessed wells, button hover fills, skeleton base
        hover: "rgba(0, 0, 23, 0.043)", // row hover in nav / menus / listboxes

        /* Text ramp, darkest to lightest. Pick by role, not by taste:
           ink        headings, primary labels, primary button fill
           ink-hover  primary button hover only
           ink-soft   emphasis text inside a panel, sub-section headers
           muted      descriptions, hints, inactive nav
           faint      meta, timestamps, counts
           placeholder input placeholders and disabled text */
        ink: "#0f0f10",
        "ink-hover": "#1c1c1d",
        "ink-soft": "#3d4148",
        muted: "#787881",
        faint: "#8a8a93",
        placeholder: "#a5a5ad",

        /* Borders, lightest to strongest. */
        line: "#eeeeef", // default hairline: panel dividers, table rows
        "line-2": "#e1e1e4", // inputs, card outlines
        "line-strong": "#d9d9dd", // hover borders, dashed empty states

        /* Status. Values are the text-legible (darker) end of each hue, so the
           same token is safe on 12px text and as a dot fill. Washes come from
           /opacity modifiers (bg-live/[0.06], border-live/25) — never a
           separate tint token. */
        live: "#15803d",
        warn: "#9a5b00",
        info: "#1d4ed8",
        danger: "#b42318",

        /* Chart series — the one place color is decorative rather than semantic,
           so it does not follow the status rules above (those tokens are the
           text-legible dark end of each hue and read as mud in a fill).

           A categorical set, keyed by the entity it paints and never by rank:
           segments drop out of the cost meter (no avatar on voice, no speech
           stack on text), so any two of the six can end up side by side and all
           fifteen pairs have to stay apart.

           ⚠️ NOT currently separation-validated. Measured on this set
           (CIEDE2000 + Viénot dichromat simulation, skipping the three pairs
           that cannot co-occur since a realtime model replaces stt/llm/tts):

             llm/analysis   ΔE 14.0 normal,  0.7 under tritanopia
             stt/platform   ΔE 23.5 normal,  5.4 under deuteranopia

           llm/analysis is the one that matters — 0.7 is the same colour, and
           those two bands sit adjacent in the calls cost breakdown. The earlier
           palette held ΔE 23.1 / 9.2 on every pair but did it with lightness,
           which is exactly what made it read as gloomy: seven cheerful hues at
           a uniform lightness cannot all separate under CVD, and that is the
           trade being made here, deliberately, pending a decision on which of
           llm or analysis moves. Every surface using these carries a text
           legend, so colour is not the sole encoding anywhere.

           Re-run the check if you touch one hue; a collapse here is invisible
           to anyone with normal colour vision. */
        chart: {
          stt: "#2e90fa", // blue
          llm: "#ff8a3d", // orange
          tts: "#22c55e", // green
          // A realtime model stands in for stt+llm+tts, and only ever appears
          // where those three do not — teal reads as their sibling, not a sixth
          // stage competing with them.
          realtime: "#06b6d4", // cyan
          avatar: "#f0518c", // pink
          // Post-call analysis is not a stage of the call — it runs once, after
          // everyone has hung up. A gold reads as "the call, plus a thing that
          // happened after" without competing with the pipeline hues.
          //
          // This pair is the whole set's tightest: llm/analysis under
          // protanopia IS the ΔE 9.2 floor quoted above, because a red-blind
          // eye pulls terracotta and gold toward the same yellow. Do not warm
          // this toward amber, or push llm further from red, without re-running
          // the all-pairs check — the collapse is invisible to normal vision.
          analysis: "#f5a524", // amber
          // A tool the caller waited on, in a latency bar. The avatar's pink on
          // purpose: the two never share a chart (one is a wait, the other a
          // billed stage), and it is the one hue here that holds against
          // stt/llm/tts on every pair — violet and cyan both collapse into the
          // STT blue (ΔE 4.5 deutan / 13.0 normal).
          tool: "#f0518c",
          platform: "#8b5cf6", // violet
        },

        /* Categorical series — for a dimension with no meaning of its own.
           An agent, a phone number, a provider: the reader needs to tell six of
           them apart and there is nothing about "agent 3" that suggests a hue.
           Distinct from `chart.*` above, which is reserved: those hues MEAN a
           billed stage of a call, and spending one on "the fourth DID" would
           make the cost meter and the volume chart look like they were about
           the same thing.

           Assigned in slot order and NEVER cycled — a ninth series folds into
           "Other" rather than reusing slot 1, so two series can never share a
           colour on one chart. Colour follows the entity, so a filter that drops
           a series must not repaint the survivors.

           Validated for stacked bars and lines (the adjacent-pair list) against
           the white panel surface: worst adjacent CVD ΔE 9.1 (protan), worst
           adjacent normal-vision ΔE 19.6. Aqua, yellow and magenta fall below
           3:1 against white, so every chart using these MUST carry a text
           legend — colour alone is not the encoding. Re-validate the whole set,
           in this order, if you touch one value: the ORDER is the safety
           mechanism, not decoration. */
        series: {
          1: "#2a78d6", // blue
          2: "#eb6834", // orange
          3: "#1baf7a", // aqua
          4: "#eda100", // yellow
          5: "#e87ba4", // magenta
          6: "#008300", // green
          7: "#4a3aa7", // violet
          8: "#e34948", // red
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        display: ["var(--font-display)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      boxShadow: {
        // resting card — barely there, hairline does the work.
        // Named `rest`, not `card`: `colors.card` exists for the landing theme,
        // and Tailwind emits a shadow-*colour* utility for every colour, so a
        // shared key makes two `.shadow-card` rules. The colour one lands last
        // and rewrites --tw-shadow to a colour that is only defined under
        // .landing-theme — which is why shadow-card drew nothing at all.
        rest: "0 1px 2px rgba(12, 13, 15, 0.04)",
        // gentle lift on hover
        soft: "0 1px 3px rgba(12, 13, 15, 0.06), 0 1px 2px rgba(12, 13, 15, 0.04)",
        // floating UI: dropdowns, popovers, toasts
        pop: "0 6px 20px -6px rgba(12, 13, 15, 0.12), 0 2px 8px -2px rgba(12, 13, 15, 0.06)",
        // modals
        lift: "0 20px 48px -16px rgba(12, 13, 15, 0.18), 0 8px 20px -8px rgba(12, 13, 15, 0.08)",
      },
      keyframes: {
        shimmer: { "100%": { backgroundPosition: "-200% 0" } },
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        "slide-up": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "none" },
        },
        // One dot of a typing indicator: it lifts and brightens, then rests for
        // the remainder of the cycle. The three dots run the same keyframes on a
        // stagger, so the lift travels across them and the long tail is what
        // separates one wave from the next.
        "typing-dot": {
          "0%, 55%, 100%": { transform: "translateY(0)", opacity: "0.32" },
          "27%": { transform: "translateY(-3.5px)", opacity: "1" },
        },
      },
      animation: {
        shimmer: "shimmer 1.3s linear infinite",
        "fade-in": "fade-in 0.15s ease-out",
        "slide-up": "slide-up 0.2s ease-out",
        "typing-dot": "typing-dot 1.15s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
