import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createRequire, syncBuiltinESMExports } from 'node:module';
import path from 'node:path';
import { mock, test } from 'node:test';
import { fileURLToPath } from 'node:url';

// A WebviewWindow emits native drops as WindowEvent, not WebviewEvent.
// Bridge E2E dispatches frontend events and cannot check this native wiring.
test('the main WebviewWindow forwards native window drag events to the source bridge', () => {
  const source = readFileSync(new URL('../apps/desktop/src/main.rs', import.meta.url), 'utf8');
  assert.ok(
    /\.on_window_event\(\|window, event\| \{[\s\S]*?tauri::WindowEvent::DragDrop\(event\)[\s\S]*?native_drop::event\(&window, event\)/.test(source),
    'Connect WindowEvent::DragDrop to native_drop::event for the main WebviewWindow.',
  );
});

test('Tauri runs in the native project without pnpm changing its working directory', async () => {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const calls = [];
  const argv = process.argv;
  const npmExecPath = process.env.npm_execpath;
  const signingNames = [
    'WENYI_UPDATER_PUBLIC_KEY', 'TAURI_SIGNING_PRIVATE_KEY', 'TAURI_SIGNING_PRIVATE_KEY_PASSWORD',
  ];
  const originalSigning = Object.fromEntries(signingNames.map(name => [name, process.env[name]]));
  for (const name of signingNames) delete process.env[name];
  const identity = { python: '1.2.3', version: '1.2.3', bundle_version: '1.2.3' };
  mock.method(childProcess, 'spawnSync', (command, parameters, options) => {
    calls.push({ command, parameters, options });
    return { status: 0, stdout: JSON.stringify(identity) };
  });
  syncBuiltinESMExports();
  process.argv = [process.execPath, 'desktop.mjs', 'build', '--bundles', 'appimage'];
  // pnpm 9 exec resolves a package root, not necessarily the supplied cwd.
  process.env.npm_execpath = path.join(root, 'mock pnpm', 'pnpm.cjs');
  try {
    const { updaterConfigured } = await import('./desktop.mjs');
    assert.equal(updaterConfigured({}, '1.2.3'), false);
    const publicKey = Buffer.from(`untrusted comment: offline fixture\n${Buffer.concat([
      Buffer.from('Ed'), Buffer.alloc(40),
    ]).toString('base64')}\n`).toString('base64');
    const signing = {
      WENYI_UPDATER_PUBLIC_KEY: publicKey,
      TAURI_SIGNING_PRIVATE_KEY: 'offline mock; never used to sign',
      TAURI_SIGNING_PRIVATE_KEY_PASSWORD: '',
    };
    assert.equal(updaterConfigured(signing, '1.2.3'), true);
    for (const partial of [
      { WENYI_UPDATER_PUBLIC_KEY: publicKey },
      { TAURI_SIGNING_PRIVATE_KEY: 'offline mock' },
      { TAURI_SIGNING_PRIVATE_KEY_PASSWORD: 'offline mock' },
      { ...signing, TAURI_SIGNING_PRIVATE_KEY_PASSWORD: undefined },
      { ...signing, WENYI_UPDATER_PUBLIC_KEY: '/outside/repository/key.pub' },
      { ...signing, WENYI_UPDATER_PUBLIC_KEY: Buffer.from('bad').toString('base64') },
    ]) assert.throws(() => updaterConfigured(partial, '1.2.3'));
    for (const version of ['1.2.3-rc.1', '1.2.3-dev.0+g123', '01.2.3', '1.2.3+local']) {
      assert.throws(() => updaterConfigured(signing, version), /stable/);
    }
    assert.equal(calls.length, 4);
    const build = calls[3];
    assert.equal(build.command, process.execPath);
    assert.deepEqual(build.parameters, [
      createRequire(import.meta.url).resolve('@tauri-apps/cli/tauri.js'),
      'build',
      '--config', path.join(root, 'apps/desktop/tauri.bundle.conf.json'),
      '--config', JSON.stringify({
        version: identity.version,
        bundle: { macOS: { bundleVersion: identity.bundle_version }, createUpdaterArtifacts: false },
        plugins: { updater: { pubkey: '' } },
      }),
      '--bundles', 'appimage',
    ]);
    assert.equal(build.options.cwd, path.join(root, 'apps/desktop'));
    assert.equal(build.options.env.WENYI_DESKTOP_VERSION, identity.version);
    assert.equal(build.options.env.WENYI_BUILD_PYTHON_VERSION, identity.python);
    Object.assign(process.env, signing);
    calls.length = 0;
    await import('./desktop.mjs?signed-build-test');
    const signedBuild = calls[3];
    const config = JSON.parse(signedBuild.parameters[5]);
    assert.equal(config.bundle.createUpdaterArtifacts, true);
    assert.equal(config.plugins?.updater?.pubkey, publicKey);
    assert.equal(signedBuild.options.env.WENYI_UPDATER_PUBLIC_KEY, publicKey);
  } finally {
    process.argv = argv;
    if (npmExecPath === undefined) delete process.env.npm_execpath;
    else process.env.npm_execpath = npmExecPath;
    for (const [name, value] of Object.entries(originalSigning)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    mock.restoreAll();
    syncBuiltinESMExports();
  }
});
