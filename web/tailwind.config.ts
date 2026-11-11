import type { Config } from "tailwindcss";

const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: v("bg"), panel: v("panel"), ink: v("ink"), muted: v("muted"), faint: v("faint"), line: v("line"),
        accent: v("accent"), "accent-ink": v("accent-ink"), "accent-soft": v("accent-soft"),
        ok: v("ok"), "ok-soft": v("ok-soft"), warn: v("warn"), "warn-soft": v("warn-soft"),
        bad: v("bad"), "bad-soft": v("bad-soft"), track: v("track"),
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      boxShadow: { card: "0 1px 2px rgb(0 0 0 / .04), 0 6px 20px rgb(0 0 0 / .05)" },
      keyframes: {
        shimmer: { "0%": { transform: "translateX(-100%)" }, "100%": { transform: "translateX(100%)" } },
        indet: { "0%": { left: "-35%" }, "100%": { left: "100%" } },
        flash: { "0%": { backgroundColor: "rgb(var(--accent-soft))" }, "100%": { backgroundColor: "transparent" } },
        pop: { "0%": { opacity: "0", transform: "translateY(4px)" }, "100%": { opacity: "1", transform: "none" } },
      },
      animation: {
        shimmer: "shimmer 1.6s ease-in-out infinite",
        indet: "indet 1.3s ease-in-out infinite",
        flash: "flash 2s ease-out",
        pop: "pop .2s ease-out",
      },
    },
  },
  plugins: [],
} satisfies Config;
