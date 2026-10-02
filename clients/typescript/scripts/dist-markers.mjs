/* The last step of every package's `npm run build`, run from that package's
 * directory (`node ../../scripts/dist-markers.mjs`).
 *
 * Each package is `"type": "module"`, so Node reads every `.js` under it as ESM
 * — including the CommonJS build, which would then fail at its first `require`.
 * A `package.json` in each output directory overrides that for the subtree, and
 * is the only way to say it: the field is per-directory, not per-file. */

import { writeFileSync } from "node:fs";
import { join } from "node:path";

for (const [dir, type] of [
  ["esm", "module"],
  ["cjs", "commonjs"],
]) {
  writeFileSync(
    join(process.cwd(), "dist", dir, "package.json"),
    JSON.stringify({ type }, null, 2) + "\n",
  );
}
