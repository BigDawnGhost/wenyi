import shared from "../../../packages/ui/tailwind.preset.js";

export default {
  presets: [shared],
  content: ["./index.html", "./src/**/*.{ts,tsx}", "../../../packages/ui/src/**/*.{ts,tsx}"],
};
