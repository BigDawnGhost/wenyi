/** Generate/check both contracts offline; temporary artifacts never enter the workspace. */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const mode = process.argv[2] ?? "--check";
if (!["--check", "--write"].includes(mode)) throw new Error("Use --check or --write.");
// The Desktop document is generated from the Desktop package, so that module
// only resolves with the workspace on PYTHONPATH.
const sources = [
  { script: "scripts/contract/export_openapi.py", env: {}, target: "packages/shared-schema/src/api.d.ts" },
  { script: "scripts/contract/export_desktop_openapi.py", env: { PYTHONPATH: "packages/core:packages/backend:apps/desktop/backend" }, target: "packages/shared-schema/src/desktop.d.ts" },
];
function run(command, args, env) {
  const result = spawnSync(command, args, { cwd: root, encoding: "utf8", env: { ...process.env, ...env } });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command} failed:\n${result.stderr}\n${result.stdout}`);
  return result.stdout;
}
const directory = mkdtempSync(join(tmpdir(), "wenyi-schema-"));
try {
  for (const source of sources) {
    const schema = JSON.parse(run("uv", ["run", "--no-sync", "python", source.script], source.env));
    const output = join(directory, "contract.d.ts");
    writeFileSync(output, COMMENT_HEADER + astToString(await openapiTS(schema)));
    const generated = readFileSync(output, "utf8");
    const committed = join(root, source.target);
    if (mode === "--write") {
      writeFileSync(committed, generated);
      console.log(`Updated ${source.target} from the offline schema.`);
    } else if (generated !== readFileSync(committed, "utf8").replaceAll("\r\n", "\n")) {
      throw new Error(`${source.target} is stale. Run pnpm gen:schema and review the diff.`);
    } else {
      console.log(`${source.target} matches the offline schema.`);
    }
  }
} finally {
  rmSync(directory, { recursive: true, force: true });
}
