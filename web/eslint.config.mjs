// ESLint flat config (ESLint 9) with Next.js's recommended rules: core web vitals, React hooks, jsx-a11y
// and typescript-eslint. Run with `npm run lint`.
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import { defineConfig, globalIgnores } from "eslint/config";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // Existing debt, reported as warnings so new errors stay visible; tighten to "error" as it is paid down.
    // - `any` in artifact renderers (the API responses the page reads are typed in src/types/api.ts).
    // - React Compiler checks: refs read during render and setState in effects are deliberate in the
    //   streaming view (smooth reveal hand-off), and Date.now() in event handlers is flagged as render-time.
    rules: {
      "@typescript-eslint/no-explicit-any": "warn",
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/refs": "warn",
      "react-hooks/purity": "warn",
      "react-hooks/immutability": "warn",
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  globalIgnores([".next/**", "out/**", "build/**", "node_modules/**", "next-env.d.ts", "tsconfig.tsbuildinfo"]),
]);
