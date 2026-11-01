/** Generate/check the shared contract offline; temporary artifacts never enter the workspace. */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const mode = process.argv[2] ?? "--check";
if (!["--check", "--write"].includes(mode)) throw new Error("Use --check or --write.");
const directory = mkdtempSync(join(tmpdir(), "wenyi-schema-"));
function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, encoding: "utf8" });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command} failed:\n${result.stderr}\n${result.stdout}`);
  return result.stdout;
}
try {
  const schema = JSON.parse(run("uv", ["run", "--no-sync", "python", "scripts/export_openapi.py"]));
  const output = join(directory, "api.d.ts");
  writeFileSync(output, COMMENT_HEADER + astToString(await openapiTS(schema)));
  const generated = readFileSync(output, "utf8");
  const committed = join(root, "packages/shared-schema/src/api.d.ts");
  if (mode === "--write") {
    writeFileSync(committed, generated);
    console.log("Updated the shared API types from the offline schema.");
  } else if (generated !== readFileSync(committed, "utf8").replaceAll("\r\n", "\n")) {
    throw new Error("Shared API types are stale. Run pnpm gen:schema and review the diff.");
  } else {
    console.log("Shared API types match the offline schema.");
  }
} finally {
  rmSync(directory, { recursive: true, force: true });
}
