// Lint settings for the page's scripts (leadgen/static/*.js). Run: npm run lint:js
// (CI runs it on every push, with the type check: tsconfig.json).
//
// The scripts are classic <script>s that load in order (templates/index.html) and share
// their top-level names. Each file lists the names it shares with the others in an
// `/* exported ... */` comment at its top; every other file may use those names and
// nothing else of it, so a misspelt, removed or renamed name fails the lint, and a
// top-level name nobody uses is reported as unused.
import { readFileSync } from "node:fs";

const DIR = "leadgen/static";
// In the order the page loads them.
const FILES = ["core.js", "leads.js", "calls.js", "find.js", "stats.js", "map.js", "start.js", "tour.js"];
// What a script uses from a library it loads itself: the Map page's Leaflet (vendor/leaflet,
// loaded by map.js the first time the Map page opens; its types are in types/page.d.ts).
const LIBRARIES = { "map.js": ["L"] };

// The browser's own names the scripts use (ES built-ins such as Map and Intl come with
// ecmaVersion).
const BROWSER = ["window", "document", "location", "history", "localStorage", "fetch", "FormData",
                 "URL", "URLSearchParams", "DOMParser", "crypto", "setTimeout", "clearTimeout",
                 "setInterval", "getComputedStyle"];

const exported = (file) => [...readFileSync(`${DIR}/${file}`, "utf8")
  .matchAll(/\/\* exported ([^*]+)\*\//g)].flatMap((m) => m[1].split(",").map((s) => s.trim()));
const shared = Object.fromEntries(FILES.map((f) => [f, exported(f)]));
const readonly = (names) => Object.fromEntries(names.map((n) => [n, "readonly"]));

export default [
  // Libraries copied in as they were released (vendor/*/README.md), not the site's own code.
  { ignores: [`${DIR}/vendor/**`] },
  ...FILES.map((file) => ({
    files: [`${DIR}/${file}`],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: readonly([...BROWSER, ...(LIBRARIES[file] || []),
                         ...FILES.filter((f) => f !== file).flatMap((f) => shared[f])]),
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      // Undefined names (a typo, or a name another file no longer shares), and assigning to
      // an undeclared name, which would make an implicit global. (Each part's other top-level
      // names are globals too, as in any classic script: the type check, tsconfig.json, fails
      // when two parts declare the same one.)
      "no-undef": "error",
      // Unused names: locals, parameters after the last one used, and top-level names not
      // listed as shared.
      "no-unused-vars": ["error", { vars: "all", args: "after-used", caughtErrors: "none" }],
      "no-redeclare": "error",
      "no-shadow-restricted-names": "error",
      "no-global-assign": "error",
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-duplicate-case": "error",
      "no-unreachable": "error",
      "no-self-assign": "error",
      "no-self-compare": "error",
      "no-unsafe-negation": "error",
      "no-unsafe-finally": "error",
      "no-cond-assign": "error",
      "no-constant-condition": ["error", { checkLoops: false }],
      "no-func-assign": "error",
      "no-const-assign": "error",
      "no-use-before-define": ["error", { functions: false, classes: false, variables: false }],
      "use-isnan": "error",
      "valid-typeof": "error",
      "eqeqeq": ["error", "always", { null: "ignore" }],
      "no-var": "error",
      "prefer-const": ["error", { destructuring: "all" }],
      // The same limit as the Python (pyproject.toml, ruff's line-length).
      "max-len": ["error", { code: 120, ignoreUrls: true }],
    },
  })),
];
