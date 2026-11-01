import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import test from "node:test";
import ts from "typescript";
import { checkHostImport, compilerOptions, imports, publicExports, root, ui } from "./source-contracts.mjs";

function sources(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry =>
    entry.isDirectory() ? sources(resolve(directory, entry.name))
      : /\.(ts|tsx)$/.test(entry.name) ? [resolve(directory, entry.name)] : []);
}

test("hosts resolve shared UI only through explicit package exports", () => {
  assert(Object.keys(publicExports).length > 5);
  for (const [key, value] of Object.entries(publicExports)) {
    assert(!key.includes("*") && !value.includes("*"), "Public exports must be explicit");
    assert(existsSync(resolve(ui, value)), `Missing export ${key}`);
    assert(!value.includes("/tests/"), "Test support is not a runtime export");
  }
  for (const host of ["apps/web", "apps/desktop/frontend"]) {
    const options = compilerOptions(host);
    assert.deepEqual(options.paths, { "@/*": ["./src/*"] });
    const files = sources(resolve(root, host, "src"));
    assert(files.length > 1, `The import scan must include ${host}`);
    for (const file of files)
      for (const specifier of imports(file)) checkHostImport(file, specifier, options);
    const vite = readFileSync(resolve(root, host, "vite.config.ts"), "utf8");
    assert(!vite.includes('"@wenyi/ui"') && !vite.includes("packages/ui/src"), "Vite must honor package exports");
  }
});

test("the import guard rejects private paths, relative escapes and alias bypasses", () => {
  const file = resolve(root, "apps/web/src/main.tsx");
  const options = compilerOptions("apps/web");
  assert.throws(() => checkHostImport(file, "@wenyi/ui/lib/api", options), /Private UI/);
  assert.throws(() => checkHostImport(file, "../../../packages/ui/src/api/client", options), /Private UI/);
  assert.throws(() => checkHostImport(file, "@private/api/client", {
    ...options, paths: { "@private/*": ["../../packages/ui/src/*"] },
  }), /Private UI/);
  assert.throws(() => checkHostImport(file, "@wenyi/ui/http", {
    ...options, paths: { "@wenyi/ui/http": ["../../packages/ui/src/api/client.ts"] },
  }), /bypasses its public export/);
});

test("shared source is host-alias independent and contract modules stay type-only", () => {
  const files = sources(resolve(ui, "src"));
  assert(files.length > 50);
  for (const file of files)
    assert(!imports(file).some(name => name.startsWith("@/")), `${file} relies on a host alias`);
  for (const name of ["api/types.ts", "platform/contracts.ts"]) {
    const file = resolve(ui, "src", name);
    const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true);
    for (const statement of source.statements) {
      if (ts.isImportDeclaration(statement)) {
        assert(statement.importClause?.isTypeOnly, `${name} imports runtime code`);
        assert(["react", "@wenyi/shared-schema", "../api/types"].includes(statement.moduleSpecifier.text));
      }
    }
  }
});

test("shared behavior suites are explicitly registered and never import a host", () => {
  const files = sources(resolve(ui, "tests/e2e"));
  const suites = files.filter(file => /export function register(?:Tests|ShellTests)\(/.test(readFileSync(file, "utf8")));
  assert(suites.length >= 20, "Scan every migrated shared suite, including the shell");
  for (const file of files) {
    const code = readFileSync(file, "utf8");
    assert(!/__WENYI_DESKTOP|__TAURI|(?:info|testInfo)\.project\.name/.test(code),
      `${file} must receive host capabilities instead of discovering a platform`);
    for (const specifier of imports(file)) {
      const target = specifier.startsWith(".") ? resolve(dirname(file), specifier) : specifier;
      assert(!/[/\\]apps[/\\]|@wenyi\/(?:web|desktop)/.test(target), `${file} imports ${specifier}`);
    }
  }
  for (const host of ["apps/web", "apps/desktop/frontend"]) {
    const directory = resolve(root, host, "tests");
    const files = sources(directory);
    assert(files.length > 20);
    const options = compilerOptions(host);
    const importedSuites = new Set();
    for (const file of files) {
      for (const specifier of imports(file)) {
        assert(!specifier.endsWith(".spec"), `${file} side-effect imports a collected suite`);
        const target = ts.resolveModuleName(specifier, file, options, ts.sys).resolvedModule?.resolvedFileName;
        if (!target) continue;
        const normalized = target.replaceAll("\\", "/");
        const other = host === "apps/web" ? "/apps/desktop/" : "/apps/web/";
        assert(!normalized.includes(other), `${file} imports the other host: ${specifier}`);
        if (suites.includes(resolve(target))) {
          assert(/\bregister(?:Tests|ShellTests)\(/.test(readFileSync(file, "utf8")),
            `${file} must explicitly register ${basename(target)}`);
          importedSuites.add(resolve(target));
        }
      }
    }
    assert.deepEqual(importedSuites, new Set(suites), `${host} must exercise every shared suite`);
    assert(!existsSync(resolve(directory, "fixtures.ts")), "Retired fixture forwarding must not return");
  }
  assert(!existsSync(resolve(ui, "src/lib/api.ts")), "Retired API forwarding must not return");
});
