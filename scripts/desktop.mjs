// Local-only launch/build orchestration. No system Python or services at runtime.
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
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
  if (command === 'pnpm' && env.npm_execpath) {
    parameters = [env.npm_execpath, ...parameters];
    command = process.execPath;
  }
  const result = spawnSync(command, parameters, { cwd, env, stdio: 'inherit' });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
run('pnpm', ['-C', 'apps/desktop/frontend', 'build']);
if (build) {
  run('uv', ['run', '--no-project', 'python', 'scripts/desktop_sidecar.py']);
  run('pnpm', ['exec', 'tauri', 'build', '--config', 'tauri.bundle.conf.json', ...args], path.join(root, 'apps/desktop'));
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
