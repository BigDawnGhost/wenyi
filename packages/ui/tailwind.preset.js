/** @type {import('tailwindcss').Config} */

// Pixel-only sizing policy: the design speaks px everywhere (no em/rem), so the
// default Tailwind scales are re-declared at their exact 16px-root values.
// Computed pixel results are identical to the rem defaults at a 16px root.
const spacingKeys = [
  0, "px", 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 3.75, 4, 4.5, 5, 6, 7, 8, 9, 10, 11, 12, 14,
  16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60, 64, 72, 80, 96, 112, 128,
  144, 160, 176, 192, 208, 224, 240, 256, 288, 320, 384,
];
const pxScale = Object.fromEntries(
  spacingKeys.map((key) => [
    key,
    key === "px" ? "1px" : key === 0 ? "0px" : `${key * 4}px`,
  ]),
);

export default {
  darkMode: ["class"],
  theme: {
    container: { center: true, padding: "32px", screens: { "2xl": "1400px" } },
    extend: {
      spacing: pxScale,
      translate: pxScale,
      fontFamily: {
        "book-quotes": ['"Wenyi Proportional Quotes"', "theme(fontFamily.sans)"],
      },
      lineHeight: {
        3: "12px",
        4: "16px",
        5: "20px",
        6: "24px",
        7: "28px",
        8: "32px",
        9: "36px",
        10: "40px",
        11: "44px",
        12: "48px",
        13: "52px",
        14: "56px",
      },
      letterSpacing: {
        tighter: "-0.8px",
        tight: "-0.4px",
        normal: "0px",
        wide: "0.4px",
        wider: "0.8px",
        widest: "1.6px",
      },
      fontSize: {
        xs: ["12px", { lineHeight: "16px" }],
        sm: ["14px", { lineHeight: "20px" }],
        base: ["16px", { lineHeight: "24px" }],
        lg: ["18px", { lineHeight: "28px" }],
        xl: ["20px", { lineHeight: "28px" }],
        "2xl": ["24px", { lineHeight: "32px" }],
        "3xl": ["30px", { lineHeight: "36px" }],
        "4xl": ["36px", { lineHeight: "40px" }],
        "5xl": ["48px", { lineHeight: "1" }],
        "6xl": ["60px", { lineHeight: "1" }],
        "7xl": ["72px", { lineHeight: "1" }],
        "8xl": ["96px", { lineHeight: "1" }],
        "9xl": ["128px", { lineHeight: "1" }],
        brand: "20px",
      },
      maxWidth: {
        xs: "320px",
        sm: "384px",
        md: "448px",
        lg: "512px",
        xl: "576px",
        "2xl": "672px",
        "3xl": "768px",
        "4xl": "896px",
        "5xl": "1024px",
        "6xl": "1152px",
        "7xl": "1280px",
      },
      width: {
        select: "140px",
        dialog: "calc(100% - 32px)",
      },
      minWidth: {
        page: "752px",
        connectivity: "560px",
      },
      minHeight: {
        dropzone: "88px",
        editor: "420px",
        synopsis: "220px",
      },
      maxHeight: {
        dialog: "90dvh",
      },
      backdropBlur: {
        xs: "2px",
      },
      colors: {
        border: "oklch(var(--border) / <alpha-value>)",
        subtle: "oklch(var(--border-subtle))",
        input: "oklch(var(--input) / <alpha-value>)",
        ring: "oklch(var(--ring) / <alpha-value>)",
        background: "oklch(var(--background) / <alpha-value>)",
        foreground: "oklch(var(--foreground) / <alpha-value>)",
        primary: {
          DEFAULT: "oklch(var(--primary) / <alpha-value>)",
          foreground: "oklch(var(--primary-foreground) / <alpha-value>)",
          hover: "oklch(var(--primary-hover) / <alpha-value>)",
          pressed: "oklch(var(--primary-pressed) / <alpha-value>)",
        },
        secondary: {
          DEFAULT: "oklch(var(--secondary) / <alpha-value>)",
          foreground: "oklch(var(--secondary-foreground) / <alpha-value>)",
          hover: "oklch(var(--secondary-hover) / <alpha-value>)",
          pressed: "oklch(var(--secondary-pressed) / <alpha-value>)",
        },
        destructive: {
          DEFAULT: "oklch(var(--destructive) / <alpha-value>)",
          foreground: "oklch(var(--destructive-foreground) / <alpha-value>)",
        },
        muted: {
          DEFAULT: "oklch(var(--muted) / <alpha-value>)",
          foreground: "oklch(var(--muted-foreground) / <alpha-value>)",
        },
        placeholder: "oklch(var(--text-placeholder))",
        accent: {
          DEFAULT: "oklch(var(--accent) / <alpha-value>)",
          foreground: "oklch(var(--accent-foreground) / <alpha-value>)",
        },
        hover: "oklch(var(--hover))",
        selected: "oklch(var(--selected))",
        surface: "oklch(var(--surface))",
        card: {
          DEFAULT: "oklch(var(--card) / <alpha-value>)",
          foreground: "oklch(var(--card-foreground) / <alpha-value>)",
          hover: "oklch(var(--card-hover) / <alpha-value>)",
          pressed: "oklch(var(--card-pressed) / <alpha-value>)",
        },
        popover: {
          DEFAULT: "oklch(var(--popover) / <alpha-value>)",
          foreground: "oklch(var(--popover-foreground) / <alpha-value>)",
        },
        "status-success": {
          DEFAULT: "oklch(var(--status-success) / <alpha-value>)",
          foreground: "oklch(var(--status-success-foreground) / <alpha-value>)",
          border: "oklch(var(--status-success-border) / <alpha-value>)",
        },
        "status-warning": {
          DEFAULT: "oklch(var(--status-warning) / <alpha-value>)",
          foreground: "oklch(var(--status-warning-foreground) / <alpha-value>)",
          border: "oklch(var(--status-warning-border) / <alpha-value>)",
        },
        "status-danger": {
          DEFAULT: "oklch(var(--status-danger) / <alpha-value>)",
          foreground: "oklch(var(--status-danger-foreground) / <alpha-value>)",
          border: "oklch(var(--status-danger-border) / <alpha-value>)",
        },
        "status-info": {
          DEFAULT: "oklch(var(--status-info) / <alpha-value>)",
          foreground: "oklch(var(--status-info-foreground) / <alpha-value>)",
          border: "oklch(var(--status-info-border) / <alpha-value>)",
        },
        warning: {
          DEFAULT: "oklch(var(--warning) / <alpha-value>)",
          foreground: "oklch(var(--warning-foreground) / <alpha-value>)",
          text: "oklch(var(--warning-text) / <alpha-value>)",
        },
        info: {
          DEFAULT: "oklch(var(--info) / <alpha-value>)",
          foreground: "oklch(var(--info-foreground) / <alpha-value>)",
        },
        chart: {
          track: "oklch(var(--chart-track) / <alpha-value>)",
          1: "oklch(var(--chart-1) / <alpha-value>)",
          2: "oklch(var(--chart-2) / <alpha-value>)",
          3: "oklch(var(--chart-3) / <alpha-value>)",
          4: "oklch(var(--chart-4) / <alpha-value>)",
          5: "oklch(var(--chart-5) / <alpha-value>)",
          6: "oklch(var(--chart-6) / <alpha-value>)",
        },
        "diff-removed": {
          DEFAULT: "oklch(var(--diff-removed-bg) / <alpha-value>)",
          foreground: "oklch(var(--diff-removed-foreground) / <alpha-value>)",
        },
        "diff-added": {
          DEFAULT: "oklch(var(--diff-added-bg) / <alpha-value>)",
          foreground: "oklch(var(--diff-added-foreground) / <alpha-value>)",
        },
      },
      borderRadius: {
        DEFAULT: "4px",
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
        xl: "12px",
        "2xl": "16px",
        "3xl": "24px",
      },
      // Elevation scale backed by the theme-switching --shadow-* variables in
      // packages/ui/src/index.css; see docs/development/design-tokens.md.
      boxShadow: {
        e0: "var(--shadow-e0)",
        e1: "var(--shadow-e1)",
        e2: "var(--shadow-e2)",
        e3: "var(--shadow-e3)",
        e4: "var(--shadow-e4)",
        "focus-ring": "var(--shadow-focus-ring)",
        sidebar: "var(--shadow-sidebar)",
        raised: "var(--shadow-raised)",
        "raised-hover": "var(--shadow-raised-hover)",
        pressed: "var(--shadow-pressed)",
        "raised-invert": "var(--shadow-raised-invert)",
        "raised-invert-hover": "var(--shadow-raised-invert-hover)",
        "pressed-invert": "var(--shadow-pressed-invert)",
      },
      transitionDuration: {
        "motion-fast": "120ms",
        "motion-normal": "200ms",
        "motion-slow": "320ms",
        sidebar: "280ms",
      },
      transitionTimingFunction: {
        standard: "cubic-bezier(0.2, 0, 0, 1)",
        decelerate: "cubic-bezier(0.05, 0.7, 0.1, 1)",
        accelerate: "cubic-bezier(0.3, 0, 0.8, 0.15)",
        sidebar: "cubic-bezier(0.22, 1, 0.36, 1)",
      },
    },
  },
  plugins: [],
};
