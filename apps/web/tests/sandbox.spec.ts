import { once } from "node:events";
import { createServer as createHttpServer } from "node:http";
import type { AddressInfo } from "node:net";
import { fileURLToPath } from "node:url";
import { expect, test, type PlaywrightTestConfig } from "@playwright/test";
import { createServer, loadConfigFromFile } from "vite";
import { createMockPlatform } from "../../../packages/ui/sandbox/mock-platform";

const appRoot = fileURLToPath(new URL("../", import.meta.url));

async function withSandbox(
  scenario: string,
  cacheDir: string,
  run: (url: string, upstreamRequests: () => number) => Promise<void>,
) {
  const previous = {
    MOCK_API: process.env.MOCK_API,
    MOCK_SCENARIO: process.env.MOCK_SCENARIO,
  };
  let upstreamRequests = 0;
  const upstream = createHttpServer((_request, response) => {
    upstreamRequests++;
    response.writeHead(502).end();
  });
  upstream.on("upgrade", (_request, socket) => {
    upstreamRequests++;
    socket.destroy();
  });
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");
  const upstreamUrl = `http://127.0.0.1:${(upstream.address() as AddressInfo).port}`;
  process.env.MOCK_API = "1";
  process.env.MOCK_SCENARIO = scenario;
  let server: Awaited<ReturnType<typeof createServer>> | undefined;
  try {
    server = await createServer({
      root: appRoot,
      configFile: `${appRoot}vite.config.ts`,
      cacheDir,
      optimizeDeps: { noDiscovery: true, include: [] },
      server: { host: "127.0.0.1", port: 0, watch: null },
      plugins: [{
        name: "sandbox-upstream-trap",
        configResolved(config) {
          // Never contact a developer's real API, even before the regression is fixed.
          for (const prefix of ["/api", "/ws"]) {
            const proxy = config.server.proxy?.[prefix];
            if (proxy && config.server.proxy) {
              config.server.proxy[prefix] = {
                ...(typeof proxy === "string" ? {} : proxy),
                target: upstreamUrl,
              };
            }
          }
        },
      }],
    });
    await server.listen();
    const port = (server.httpServer!.address() as AddressInfo).port;
    await run(`http://127.0.0.1:${port}`, () => upstreamRequests);
  } finally {
    await server?.close();
    upstream.close();
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

for (const [scenario, method, path, status] of [
  ["unauthorized", "GET", "/projects", 401],
  ["error500", "POST", "/projects/book-1/translate", 500],
] as const) {
  test(`offline platform applies the ${scenario} HTTP fault`, async () => {
    const platform = createMockPlatform({ scenario });
    await expect(platform.request(path, { method })).rejects.toMatchObject({ status });
  });

  test(`mock middleware applies the ${scenario} HTTP fault`, async ({}, testInfo) => {
    await withSandbox(scenario, testInfo.outputPath("vite-cache"), async (url, upstreamRequests) => {
      const response = await fetch(`${url}/api${path}`, { method });
      expect(response.status).toBe(status);
      expect(await response.json()).toHaveProperty("detail");
      expect(upstreamRequests()).toBe(0);
    });
  });
}

test("mock progress stays connected and never reaches the real WebSocket proxy", async ({}, testInfo) => {
  await withSandbox("translating", testInfo.outputPath("vite-cache"), async (url, upstreamRequests) => {
    const socket = new WebSocket(`${url.replace("http:", "ws:")}/ws/projects/book-1/progress`);
    const frames: { kind: string; project_id?: string; run_id?: string }[] = [];
    socket.addEventListener("open", () => socket.send(JSON.stringify({ token: "" })));
    socket.addEventListener("message", event => frames.push(JSON.parse(String(event.data))));
    try {
      await expect.poll(() => frames.filter(frame => frame.kind === "translation").length)
        .toBeGreaterThanOrEqual(2);
      expect(frames[0]).toMatchObject({ kind: "snapshot", project_id: "book-1" });
      expect(frames.filter(frame => frame.kind === "translation")).toEqual(
        expect.arrayContaining([expect.objectContaining({ project_id: "book-1", run_id: "run-a" })]),
      );
      expect(socket.readyState).toBe(WebSocket.OPEN);
      expect(upstreamRequests()).toBe(0);
    } finally {
      socket.close();
    }
  });
});

for (const { selection, perf, expected } of [
  { selection: "", perf: "", expected: ["web"] },
  { selection: "web-webkit", perf: "", expected: ["web-webkit"] },
  { selection: "web, web-webkit", perf: "", expected: ["web", "web-webkit"] },
  { selection: "web-webkit", perf: "1", expected: ["web", "web-webkit", "performance"] },
]) {
  test(`browser project selection ${selection || "default"} with performance ${perf || "off"}`, async () => {
    const previous = {
      PLAYWRIGHT_PROJECTS: process.env.PLAYWRIGHT_PROJECTS,
      PLAYWRIGHT_PERF: process.env.PLAYWRIGHT_PERF,
    };
    process.env.PLAYWRIGHT_PROJECTS = selection;
    process.env.PLAYWRIGHT_PERF = perf;
    try {
      const loaded = await loadConfigFromFile(
        { command: "serve", mode: "test" },
        `${appRoot}playwright.config.ts`,
      );
      // Vite transpiles the TypeScript file; its contents use Playwright's schema.
      const config = loaded?.config as unknown as PlaywrightTestConfig;
      expect(config.projects?.map(project => project.name)).toEqual(expected);
    } finally {
      for (const [key, value] of Object.entries(previous)) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
    }
  });
}
