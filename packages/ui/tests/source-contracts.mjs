import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

export const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
export const ui = resolve(root, "packages/ui");
export const publicExports = JSON.parse(readFileSync(resolve(ui, "package.json"), "utf8")).exports;

export function imports(file) {
  return ts.preProcessFile(readFileSync(file, "utf8"), true, true).importedFiles.map(item => item.fileName);
}

export function compilerOptions(host) {
  const path = resolve(root, host, "tsconfig.json");
  const config = ts.readConfigFile(path, ts.sys.readFile);
  assert(!config.error, `Cannot read ${path}`);
  return ts.parseJsonConfigFileContent(config.config, ts.sys, dirname(path)).options;
}

export function checkHostImport(file, specifier, options) {
  const exported = specifier.startsWith("@wenyi/ui/");
  const target = ts.resolveModuleName(specifier, file, options, ts.sys).resolvedModule?.resolvedFileName;
  if (exported) {
    const entry = publicExports[`./${specifier.slice("@wenyi/ui/".length)}`];
    assert(entry, `Private UI import: ${specifier}`);
    // CSS is resolved by Vite, not TypeScript. Its exact public export is still checked.
    if (!entry.endsWith(".css")) {
      assert.equal(target && resolve(target), resolve(ui, entry), `${specifier} bypasses its public export`);
    }
  } else if (target) {
    const sharedSource = resolve(ui, "src").replaceAll("\\", "/") + "/";
    const normalized = target.replaceAll("\\", "/");
    assert(!normalized.startsWith(sharedSource), `Private UI import: ${specifier}`);
    assert(!normalized.includes("/tests/"), `Runtime imports test support: ${specifier}`);
    const host = file.replaceAll("\\", "/").includes("/apps/web/") ? "/apps/desktop/" : "/apps/web/";
    assert(!normalized.includes(host), `Cross-host import: ${specifier}`);
  }
}

/** Resolve only literal route loaders; importing the manifest would execute React/browser code. */
export function routeModules() {
  const path = resolve(ui, "src/routes/manifest.tsx");
  const source = ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true);
  const loaders = new Map();
  let names;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
      if (node.name.text === "pageLoaders") {
        assert(node.initializer && ts.isArrayLiteralExpression(node.initializer));
        names = node.initializer.elements.map(item => {
          assert(ts.isIdentifier(item));
          return item.text;
        });
      } else if (node.name.text.startsWith("load") && node.initializer) {
        const targets = [];
        function findImport(child) {
          if (ts.isCallExpression(child) && child.expression.kind === ts.SyntaxKind.ImportKeyword) {
            assert(ts.isStringLiteral(child.arguments[0]));
            targets.push(child.arguments[0].text);
          }
          ts.forEachChild(child, findImport);
        }
        findImport(node.initializer);
        assert.equal(targets.length, 1, `Expected one literal import in ${node.name.text}`);
        loaders.set(node.name.text, targets[0]);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert(names?.length, "The route-loader scan must not be empty");
  assert.deepEqual(new Set(names), new Set(loaders.keys()), "Warm-up must cover every route loader");
  assert.equal(new Set(names).size, names.length, "Route loaders must not repeat");
  return names.map(name => loaders.get(name));
}
