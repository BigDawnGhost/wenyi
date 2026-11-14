import assert from "node:assert/strict";
import test from "node:test";
import { warmPages } from "../src/routes/warmup.ts";

const settle = async () => {
  for (let index = 0; index < 10; index++) await Promise.resolve();
};

function setup(t, idleSupported = true) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let foreground = true;
  let changed;
  let callback;
  const host = idleSupported ? {
    requestIdleCallback: (run) => { callback = run; return 1; },
    cancelIdleCallback: () => { callback = undefined; },
  } : {};
  const previous = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", { value: host, configurable: true });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, "window", previous);
    else delete globalThis.window;
  });
  return {
    activity: {
      isForeground: () => foreground,
      subscribe: (listener) => {
        changed = listener;
        return () => { changed = undefined; };
      },
    },
    visible: (next) => { foreground = next; changed?.(); },
    idle: () => { const run = callback; callback = undefined; run?.(); },
  };
}

test("warm-up yields first paint and never loads two pages concurrently", async (t) => {
  const host = setup(t);
  const calls = [];
  let resolve;
  const cancel = warmPages([
    () => { calls.push(1); return new Promise((done) => { resolve = done; }); },
    async () => { calls.push(2); },
  ], host.activity);
  await settle();
  assert.deepEqual(calls, []);
  t.mock.timers.tick(1000);
  assert.deepEqual(calls, []);
  host.idle();
  await settle();
  assert.deepEqual(calls, [1]);
  t.mock.timers.tick(10_000);
  host.idle();
  assert.deepEqual(calls, [1]);
  resolve();
  await settle();
  t.mock.timers.tick(1000);
  host.idle();
  await settle();
  assert.deepEqual(calls, [1, 2]);
  cancel();
});

test("hiding and cleanup cancel queued work; a failure does not stop other pages", async (t) => {
  const host = setup(t);
  const calls = [];
  const cancel = warmPages([
    async () => { calls.push(1); throw new Error("Chunk unavailable"); },
    async () => { calls.push(2); },
  ], host.activity);
  t.mock.timers.tick(1000);
  host.visible(false);
  host.idle();
  await settle();
  assert.deepEqual(calls, []);
  host.visible(true);
  t.mock.timers.tick(1000);
  host.idle();
  await settle();
  assert.deepEqual(calls, [1]);
  t.mock.timers.tick(1000);
  cancel();
  host.idle();
  host.visible(true);
  t.mock.timers.tick(10_000);
  await settle();
  assert.deepEqual(calls, [1]);
});

test("hosts without idle callbacks still space out page loading", async (t) => {
  const host = setup(t, false);
  const calls = [];
  const cancel = warmPages([
    async () => { calls.push(1); throw new Error("Chunk unavailable"); },
    async () => { calls.push(2); },
  ], host.activity);
  t.mock.timers.tick(1000);
  await settle();
  assert.deepEqual(calls, [1]);
  t.mock.timers.tick(999);
  await settle();
  assert.deepEqual(calls, [1]);
  t.mock.timers.tick(1);
  await settle();
  assert.deepEqual(calls, [1, 2]);
  cancel();
});
