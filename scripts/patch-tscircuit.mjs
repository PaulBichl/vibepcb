// Re-applies VibePCB's local fixes to node_modules (runs as npm `postinstall`; `npm install` drops them).
//
//   node scripts/patch-tscircuit.mjs            apply the patches that are missing (never fails an install)
//   node scripts/patch-tscircuit.mjs --check    only report; exit 1 if a patch is missing or does not match
//   node scripts/patch-tscircuit.mjs --root DIR node_modules lives in DIR (default: the current directory)
//
// patches/tscircuit.json lists {file, old, new}: one exact text replacement per file. patch-package is not
// used because the dist bundles are minified (one line = 50 KB to 3 MB, so its line diffs would be megabytes).
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const check = args.includes("--check");
const rootIndex = args.indexOf("--root");
const root = resolve(rootIndex >= 0 ? args[rootIndex + 1] : process.cwd());
const spec = JSON.parse(readFileSync(join(here, "..", "patches", "tscircuit.json"), "utf8"));

let applied = 0;
let already = 0;
const problems = [];
for (const { file, old, new: replacement } of spec.patches) {
  const path = join(root, "node_modules", file);
  if (!existsSync(path)) {
    problems.push(`${file}: not installed`);
    continue;
  }
  const text = readFileSync(path, "utf8");
  const first = text.indexOf(old);
  if (first >= 0 && text.indexOf(old, first + 1) < 0) {
    if (check) {
      problems.push(`${file}: patch missing`);
    } else {
      writeFileSync(path, text.slice(0, first) + replacement + text.slice(first + old.length));
      applied += 1;
    }
  } else if (text.includes(replacement)) {
    already += 1;
  } else {
    problems.push(`${file}: does not match (another tscircuit version than ${spec.package}?)`);
  }
}
console.log(`tscircuit patches: ${applied} applied, ${already} already in place, ${problems.length} problem(s)`);
for (const p of problems) console.log(`  ${p}`);
process.exit(check && problems.length > 0 ? 1 : 0);
