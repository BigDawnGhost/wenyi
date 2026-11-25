import { type Dirent } from "node:fs";
import { readFile, readdir, realpath, writeFile } from "node:fs/promises";
import path from "node:path";
import type { Plugin } from "vite";

type PackageJson = Record<string, unknown>;

interface PackageNotice {
  name: string;
  version: string;
  license: string;
  homepage: string | null;
  author: string | null;
}

interface WalkContext {
  appNodeModules: string;
  notices: Map<string, PackageNotice>;
  visited: Set<string>;
}

const FIRST_PARTY_SCOPE = "@wenyi/";
const DEPENDENCY_FIELDS = ["dependencies", "optionalDependencies"] as const;

function asString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function licenseOf(pkg: PackageJson): string {
  const license = asString(pkg.license);
  if (license) return license;
  if (Array.isArray(pkg.licenses)) {
    const names = pkg.licenses
      .map((entry) =>
        entry !== null && typeof entry === "object" ? asString((entry as PackageJson).type) : asString(entry),
      )
      .filter((name): name is string => name !== null);
    if (names.length > 0) return names.join(" OR ");
  }
  return "UNKNOWN";
}

function personName(value: unknown): string | null {
  if (typeof value === "string") return asString(value);
  if (value !== null && typeof value === "object") return asString((value as PackageJson).name);
  return null;
}

function repositoryUrl(pkg: PackageJson): string | null {
  if (typeof pkg.repository === "string") return asString(pkg.repository);
  if (pkg.repository !== null && typeof pkg.repository === "object") {
    return asString((pkg.repository as PackageJson).url);
  }
  return null;
}

async function readPackageJson(packageDir: string): Promise<PackageJson | null> {
  try {
    const parsed: unknown = JSON.parse(await readFile(path.join(packageDir, "package.json"), "utf8"));
    return parsed !== null && typeof parsed === "object" ? (parsed as PackageJson) : null;
  } catch {
    return null;
  }
}

async function listDirectories(dir: string): Promise<Dirent[]> {
  try {
    return await readdir(dir, { withFileTypes: true });
  } catch {
    return [];
  }
}

function dependencyNames(pkg: PackageJson): string[] {
  const names: string[] = [];
  for (const field of DEPENDENCY_FIELDS) {
    const block = pkg[field];
    if (block !== null && typeof block === "object") {
      names.push(...Object.keys(block as PackageJson));
    }
  }
  return names;
}

function recordNotice(pkg: PackageJson, notices: Map<string, PackageNotice>): void {
  const name = asString(pkg.name);
  const version = asString(pkg.version);
  if (!name || !version || name.startsWith(FIRST_PARTY_SCOPE) || pkg.private === true) return;
  const key = `${name}@${version}`;
  if (notices.has(key)) return;
  notices.set(key, {
    name,
    version,
    license: licenseOf(pkg),
    homepage: asString(pkg.homepage) ?? repositoryUrl(pkg),
    author: personName(pkg.author),
  });
}

async function resolveDependency(
  context: WalkContext,
  fromPackageDir: string,
  name: string,
): Promise<string | null> {
  const bases = [fromPackageDir, path.dirname(context.appNodeModules)];
  for (const base of bases) {
    try {
      return await realpath(path.join(base, "node_modules", name));
    } catch {
      continue;
    }
  }
  return null;
}

async function walkClosure(context: WalkContext, startDirs: string[]): Promise<void> {
  const queue = [...startDirs];
  while (queue.length > 0) {
    const packageDir = queue.pop();
    if (!packageDir || context.visited.has(packageDir)) continue;
    context.visited.add(packageDir);
    const pkg = await readPackageJson(packageDir);
    if (!pkg) continue;
    recordNotice(pkg, context.notices);
    for (const depName of dependencyNames(pkg)) {
      const resolved = await resolveDependency(context, packageDir, depName);
      if (resolved && !context.visited.has(resolved)) queue.push(resolved);
    }
  }
}

async function directDependencyDirs(nodeModulesDir: string): Promise<string[]> {
  const candidates: string[] = [];
  for (const entry of await listDirectories(nodeModulesDir)) {
    if (entry.name.startsWith(".")) continue;
    if (entry.name.startsWith("@")) {
      for (const scoped of await listDirectories(path.join(nodeModulesDir, entry.name))) {
        candidates.push(path.join(nodeModulesDir, entry.name, scoped.name));
      }
      continue;
    }
    candidates.push(path.join(nodeModulesDir, entry.name));
  }
  const resolved: string[] = [];
  for (const candidate of candidates) {
    try {
      resolved.push(await realpath(candidate));
    } catch {
      continue;
    }
  }
  return resolved;
}

function compareNotices(a: PackageNotice, b: PackageNotice): number {
  const byName = a.name.localeCompare(b.name);
  return byName !== 0 ? byName : a.version.localeCompare(b.version);
}

function renderNotices(notices: PackageNotice[]): string {
  const unknownCount = notices.filter((notice) => notice.license === "UNKNOWN").length;
  const lines = [
    "# Third-Party Software Notices",
    "",
    "Generated at build time by `scripts/third-party-notices.ts`. Do not edit by hand;",
    "regenerate with the production build (`pnpm -C <app> build`).",
    "",
    "This manifest lists the npm dependency closure installed for this application at",
    "build time, including development-only packages, together with the license each",
    "package declares in its package.json. Full license texts are available inside",
    "each installed package (node_modules/<package>/LICENSE*) and from its source",
    "repository or homepage.",
    "",
    `Total packages: ${notices.length}`,
    "",
  ];
  if (unknownCount > 0) {
    lines.push(
      `${unknownCount} package(s) declare no machine-readable license and are listed as`,
      "UNKNOWN. Review them before distributing a release build.",
      "",
    );
  }
  for (const notice of notices) {
    lines.push(`## ${notice.name} ${notice.version}`, "", `- License: ${notice.license}`);
    if (notice.homepage) lines.push(`- Homepage: ${notice.homepage}`);
    if (notice.author) lines.push(`- Author: ${notice.author}`);
    lines.push("");
  }
  return lines.join("\n");
}

export function thirdPartyNotices(): Plugin {
  let root = process.cwd();
  let outDir = "dist";
  return {
    name: "wenyi-third-party-notices",
    apply: "build",
    configResolved(config) {
      root = config.root;
      outDir = config.build.outDir;
    },
    async closeBundle() {
      const appNodeModules = path.resolve(root, "node_modules");
      const context: WalkContext = { appNodeModules, notices: new Map(), visited: new Set() };
      await walkClosure(context, await directDependencyDirs(appNodeModules));
      if (context.notices.size === 0) {
        this.warn("third-party-notices: no installed packages found; manifest will be empty");
      }
      const manifest = renderNotices([...context.notices.values()].sort(compareNotices));
      await writeFile(path.resolve(root, outDir, "THIRD-PARTY-NOTICES.md"), manifest, "utf8");
    },
  };
}
