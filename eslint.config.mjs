// Lint for the scripts under gui/static — the JavaScript counterpart of the
// `ruff --select E4,E7,E9,F` run on the Python: mistakes, not style.
//
// These scripts are served as written. Nothing compiles or bundles them and no
// test loads them, so a slip in one reaches a browser unnoticed. That is how a
// commit that deleted one `const` shipped a setup wizard which could neither be
// saved nor skipped: the name was still used in two places, and the first
// anyone knew was a ReferenceError on the Finish button. `no-undef` reports
// that on the line it happens.
//
// Run it the way CI does (.github/workflows/tests.yml):
//
//     npx --yes eslint@10.12.0 gui/static
//
// Deliberately free of imports, so there is no package.json and nothing to
// install: this stays a Python project. The price is that the browser globals
// below are listed by hand — add to them when a script starts using another.

const browser = Object.fromEntries([
  "window", "document", "console", "fetch", "alert",
  "setTimeout", "clearTimeout", "setInterval", "clearInterval",
  "URLSearchParams", "WebSocket", "IntersectionObserver",
].map((name) => [name, "readonly"]));

export default [
  {
    files: ["gui/static/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",      // plain <script> tags, not modules
      globals: browser,
    },
    linterOptions: {
      reportUnusedDisableDirectives: "error",
    },
    rules: {
      // A name that is used but declared nowhere.
      "no-undef": "error",
      // ...and the reverse. Unused parameters are left alone: handlers take
      // arguments they ignore.
      "no-unused-vars": ["error", { args: "none", caughtErrors: "none" }],

      // Code that cannot do what it reads as doing.
      "no-const-assign": "error",
      "no-redeclare": "error",
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-dupe-else-if": "error",
      "no-duplicate-case": "error",
      "no-func-assign": "error",
      "no-self-assign": "error",
      "no-self-compare": "error",
      "no-unreachable": "error",
      "no-unsafe-finally": "error",
      "no-unsafe-negation": "error",
      "no-unsafe-optional-chaining": "error",
      "no-cond-assign": "error",
      "no-constant-binary-expression": "error",
      "no-constant-condition": "error",
      "no-fallthrough": "error",
      "no-global-assign": "error",
      "no-invalid-regexp": "error",
      "no-loss-of-precision": "error",
      "no-obj-calls": "error",
      "no-shadow-restricted-names": "error",
      "no-sparse-arrays": "error",
      "no-unused-labels": "error",
      "for-direction": "error",
      "getter-return": "error",
      "use-isnan": "error",
      "valid-typeof": "error",
    },
  },
];
