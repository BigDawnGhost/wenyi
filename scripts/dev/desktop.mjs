// Local-only launch/build orchestration. No system Python or services at runtime.
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const build = process.argv[2] === 'build';
const args = process.argv.slice(3);
const env = { ...process.env };
export function updaterConfigured(environment, version) {
  const publicKey = environment.WENYI_UPDATER_PUBLIC_KEY?.trim();
  const privateKey = environment.TAURI_SIGNING_PRIVATE_KEY?.trim();
  const password = environment.TAURI_SIGNING_PRIVATE_KEY_PASSWORD;
  if ((!publicKey && !privateKey && !password)) return false;
  if (!publicKey || !privateKey || password === undefined) {
    throw new Error('Updater signing requires WENYI_UPDATER_PUBLIC_KEY, TAURI_SIGNING_PRIVATE_KEY and TAURI_SIGNING_PRIVATE_KEY_PASSWORD (which may be empty).');
  }
  const decoded = Buffer.from(publicKey, 'base64').toString('utf8');
  const lines = decoded.trim().split(/\r?\n/);
  const packet = Buffer.from(lines[1] ?? '', 'base64');
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(publicKey)
    || lines.length !== 2 || !lines[0].startsWith('untrusted comment:')
    || packet.length !== 42 || !['Ed', 'ED'].includes(packet.subarray(0, 2).toString())) {
    throw new Error('WENYI_UPDATER_PUBLIC_KEY must contain the Tauri minisign public key content, not a path.');
  }
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version)) {
    throw new Error('Updater signing is supported only for stable release versions.');
  }
  return true;
}
// A workspace-local Rust toolchain is optional; never change global defaults.
if (!env.RUSTUP_HOME && existsSync(path.join(root, '.scratch/rustup/toolchains'))) {
  env.RUSTUP_HOME = path.join(root, '.scratch/rustup');
  env.CARGO_HOME = path.join(root, '.scratch/cargo');
}
function run(command, parameters, cwd = root, commandEnv = env) {
  let shell = false;
  if (command === 'pnpm' && commandEnv.npm_execpath) {
    if (/\.[cm]?js$/i.test(commandEnv.npm_execpath)) {
      parameters = [commandEnv.npm_execpath, ...parameters];
      command = process.execPath;
    } else {
      // Standalone pnpm binaries may have no extension (for example, Linux ELF).
      command = commandEnv.npm_execpath;
    }
  } else if (command === 'pnpm' && process.platform === 'win32') {
    // No npm_execpath (direct node invocation): pnpm may exist only as a .cmd shim.
    shell = true;
  }
  const result = spawnSync(command, parameters, { cwd, env: commandEnv, stdio: 'inherit', shell });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function installedDesktopVersion(python) {
  const result = spawnSync(python, ['-I', '-c', [
    'from importlib.metadata import PackageNotFoundError, version',
    'try:',
    '    print(version("wenyi-desktop"))',
    'except PackageNotFoundError:',
    '    pass',
  ].join('\n')], { cwd: root, env, encoding: 'utf8' });
  if (result.status !== 0) throw new Error('Could not read version metadata from the workspace Python.');
  return result.stdout.trim();
}

function workspacePython(expected) {
  const venv = path.join(root, '.venv');
  const python = path.join(venv, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  if (!existsSync(python)) {
    throw new Error('Install Desktop dependencies with uv sync --locked --package wenyi-desktop --group dev first.');
  }
  if (installedDesktopVersion(python) !== expected) {
    // Editable installs retain metadata from the time they were built, including
    // the previous Git revision or dirty state. Refresh the selected workspace
    // environment without pruning packages installed for other workspace apps.
    run('uv', [
      'sync', '--inexact', '--locked', '--package', 'wenyi-desktop', '--group', 'dev',
      '--reinstall-package', 'wenyi-desktop', '--python', python,
    ], root, { ...env, UV_PROJECT_ENVIRONMENT: venv });
    if (installedDesktopVersion(python) !== expected) {
      throw new Error('Workspace Python still has a different Desktop version after refresh. Check the checkout and retry.');
    }
  }
  return python;
}

const identity = spawnSync('uv', [
  'run', '--no-project', '--with', 'hatch-vcs', 'python', 'scripts/release/release_version.py',
], { cwd: root, env, encoding: 'utf8' });
if (identity.status !== 0) {
  console.error(identity.stderr);
  process.exit(identity.status ?? 1);
}
const version = JSON.parse(identity.stdout);
const signedUpdater = build && updaterConfigured(env, version.version);
if (build && !signedUpdater) console.log('Updater signing is not configured; building manual installers only (no latest.json).');
env.WENYI_DESKTOP_VERSION = version.version;
env.WENYI_BUILD_PYTHON_VERSION = version.python;
const versionConfig = JSON.stringify({
  version: version.version,
  bundle: { macOS: { bundleVersion: version.bundle_version }, createUpdaterArtifacts: signedUpdater },
  // Tauri's bundler needs the same public key as the native runtime to sign updates.
  plugins: { updater: { pubkey: env.WENYI_UPDATER_PUBLIC_KEY?.trim() || '' } },
});
env.TAURI_CONFIG = versionConfig;
if (!build && !env.WENYI_DESKTOP_PYTHON) {
  env.WENYI_DESKTOP_PYTHON = workspacePython(version.python);
}
console.log(`Wenyi Desktop ${version.version} (Python ${version.python})`);
run('pnpm', ['-C', 'apps/desktop/frontend', 'build']);
if (build) {
  run('uv', ['run', '--no-project', 'python', 'scripts/dev/desktop_sidecar.py']);
  // pnpm 9 exec changes cwd to the nearest package root, outside the native project.
  const tauriCli = createRequire(import.meta.url).resolve('@tauri-apps/cli/tauri.js');
  run(process.execPath, [tauriCli, 'build',
    '--config', path.join(root, 'apps/desktop/tauri.bundle.conf.json'),
    '--config', versionConfig, ...args], path.join(root, 'apps/desktop'));
} else {
  run('cargo', ['run', '--locked', '--manifest-path', 'apps/desktop/Cargo.toml', '--', ...args]);
}
