import { createServer } from "node:http";
import { test as base } from "@playwright/test";

type UploadResponse = { status?: number; json: unknown };
type UploadHandler = (form: FormData) => UploadResponse | Promise<UploadResponse>;

export const test = base.extend<{ captureUpload: (handler: UploadHandler) => Promise<void> }>({
  captureUpload: async ({ page, baseURL }, use) => {
    if (!baseURL) throw new Error("Upload fixture requires a baseURL");
    const origin = new URL(baseURL).origin;
    let handler: UploadHandler | undefined;
    const server = createServer(async (request, response) => {
      response.setHeader("access-control-allow-origin", origin);
      try {
        const chunks: Buffer[] = [];
        for await (const chunk of request) chunks.push(Buffer.from(chunk));
        const form = await new Response(Buffer.concat(chunks), {
          headers: { "content-type": request.headers["content-type"]! },
        }).formData();
        const result = await handler!(form);
        response.writeHead(result.status ?? 200, { "content-type": "application/json" });
        response.end(JSON.stringify(result.json));
      } catch (error) {
        response.writeHead(500, { "content-type": "application/json" });
        response.end(JSON.stringify({ detail: String(error) }));
      }
    });
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("Upload fixture did not bind");
    // WebKit's intercepted postDataBuffer omits file bytes. Forward the original request
    // unchanged to a loopback receiver so assertions cover what the browser actually sends.
    try {
      await use(async respond => {
        handler = respond;
        await page.route("**/api/projects", route =>
          route.request().method() === "POST"
            ? route.continue({ url: `http://127.0.0.1:${address.port}/upload` })
            : route.fallback(),
        );
      });
    } finally {
      await new Promise<void>((resolve, reject) =>
        server.close(error => error ? reject(error) : resolve()));
    }
  },
});
