import assert from "node:assert/strict";
import { readdirSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
// Match Vite's default warning threshold, measured in uncompressed decimal kB.
const chunkSizeLimit = 500_000;

for (const app of ["apps/web", "apps/desktop/frontend"]) {
  test(`${app} production JavaScript chunks stay within 500 kB`, () => {
    const assets = resolve(root, app, "dist/assets");
    const chunks = readdirSync(assets).filter((file) => file.endsWith(".js"));
    assert(chunks.length > 0, `Build ${app} before checking chunk sizes`);
    const oversized = chunks
      .map((file) => ({ file, bytes: statSync(resolve(assets, file)).size }))
      .filter(({ bytes }) => bytes > chunkSizeLimit);
    assert.deepEqual(oversized, [], "Split oversized chunks instead of raising the warning limit");
  });
}
