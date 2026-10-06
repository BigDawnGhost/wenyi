// Local-only launch/build orchestration. No system Python or services at runtime.
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const build = process.argv[2] === 'build';
const args = process.argv.slice(3);
const env = { ...process.env };
// A workspace-local Rust toolchain is optional; never change global defaults.
if (!env.RUSTUP_HOME && existsSync(path.join(root, '.scratch/rustup/toolchains'))) {
  env.RUSTUP_HOME = path.join(root, '.scratch/rustup');
  env.CARGO_HOME = path.join(root, '.scratch/cargo');
}
function run(command, parameters, cwd = root) {
  let shell = false;
  if (command === 'pnpm' && env.npm_execpath) {
    if (env.npm_execpath.endsWith('.exe')) {
      // Native pnpm binary; Node cannot load it as a module.
      command = env.npm_execpath;
    } else {
      parameters = [env.npm_execpath, ...parameters];
      command = process.execPath;
    }
  } else if (command === 'pnpm' && process.platform === 'win32') {
    // No npm_execpath (direct node invocation): pnpm may exist only as a .cmd shim.
    shell = true;
  }
  const result = spawnSync(command, parameters, { cwd, env, stdio: 'inherit', shell });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
const identity = spawnSync('uv', [
  'run', '--no-project', '--with', 'hatch-vcs', 'python', 'scripts/release_version.py',
], { cwd: root, env, encoding: 'utf8' });
if (identity.status !== 0) {
  console.error(identity.stderr);
  process.exit(identity.status ?? 1);
}
const version = JSON.parse(identity.stdout);
env.WENYI_DESKTOP_VERSION = version.version;
env.WENYI_BUILD_PYTHON_VERSION = version.python;
const versionConfig = JSON.stringify({
  version: version.version,
  bundle: { macOS: { bundleVersion: version.bundle_version } },
});
env.TAURI_CONFIG = versionConfig;
console.log(`Wenyi Desktop ${version.version} (Python ${version.python})`);
run('pnpm', ['-C', 'apps/desktop/frontend', 'build']);
if (build) {
  run('uv', ['run', '--no-project', 'python', 'scripts/desktop_sidecar.py']);
  // pnpm 9 exec changes cwd to the nearest package root, outside the native project.
  const tauriCli = createRequire(import.meta.url).resolve('@tauri-apps/cli/tauri.js');
  run(process.execPath, [tauriCli, 'build',
    '--config', path.join(root, 'apps/desktop/tauri.bundle.conf.json'),
    '--config', versionConfig, ...args], path.join(root, 'apps/desktop'));
} else {
  if (!env.WENYI_DESKTOP_PYTHON) {
    const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    if (!existsSync(python)) {
      console.error('Install Desktop dependencies with uv sync --locked --package wenyi-desktop --group dev first.');
      process.exit(1);
    }
    env.WENYI_DESKTOP_PYTHON = python;
  }
  run('cargo', ['run', '--locked', '--manifest-path', 'apps/desktop/Cargo.toml', '--', ...args]);
}
