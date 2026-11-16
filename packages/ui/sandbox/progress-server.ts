/** Minimal RFC 6455 WebSocket endpoint for `/ws/projects/{pid}/progress`,
 * attached by the MOCK_API dev middleware so live progress works without a
 * backend. Dev-only: never imported by an application entry. */

import { createHash } from "node:crypto";
import type { Server as NodeHttpServer, IncomingMessage } from "node:http";
import type { Http2SecureServer } from "node:http2";
import type { Socket as NetSocket } from "node:net";
import type { Duplex } from "node:stream";
import type { SandboxState } from "./fixtures";
import { snapshotFrame, tickFrame } from "./progress-frames";

const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/** Vite may run an HTTP/1.1 or an HTTP/2 server; only the former carries the
 * WebSocket upgrades this simulator answers, so the narrow shape is used at
 * the listener site. */
type DevHttpServer = NodeHttpServer | Http2SecureServer;

/** Answer `/ws/projects/{pid}/progress` upgrades on the dev server. */
export function attachProgressServer(server: DevHttpServer, sandbox: SandboxState): void {
  const http1 = server as NodeHttpServer;
  http1.on("upgrade", (req: IncomingMessage, socket: Duplex, head: Buffer) => {
    const match = (req.url ?? "").match(/^\/ws\/projects\/([^/]+)\/progress$/);
    const key = req.headers["sec-websocket-key"];
    if (!match || typeof key !== "string") return;
    socket.write(
      "HTTP/1.1 101 Switching Protocols\r\n" +
        "Upgrade: websocket\r\n" +
        "Connection: Upgrade\r\n" +
        `Sec-WebSocket-Accept: ${createHash("sha1").update(key + WS_GUID).digest("base64")}\r\n` +
        "\r\n",
    );
    (socket as NetSocket).setNoDelay(true);
    simulate(decodeURIComponent(match[1]), socket, sandbox, Buffer.from(head));
  });
}

function simulate(projectId: string, socket: Duplex, sandbox: SandboxState, initial: Buffer): void {
  let buffer = initial;
  let authenticated = false;
  let terminalSent = false;
  let done: number | null = null;
  let lastFrameAt = Date.now();
  let ticker: ReturnType<typeof setInterval> | undefined;

  const send = (frame: Record<string, unknown>) => {
    socket.write(encodeFrame(Buffer.from(JSON.stringify(frame), "utf8")));
    lastFrameAt = Date.now();
  };
  const stop = () => {
    if (ticker) clearInterval(ticker);
    ticker = undefined;
  };

  socket.on("data", (chunk: Buffer) => {
    buffer = Buffer.concat([buffer, chunk]);
    consume();
  });
  socket.on("error", stop);
  socket.on("close", stop);

  function consume(): void {
    const { frames, rest } = parseClientFrames(buffer);
    buffer = rest;
    for (const frame of frames) {
      if (frame.opcode === 0x8) {
        stop();
        socket.end();
        return;
      }
      if (frame.opcode === 0x9) {
        socket.write(encodeFrame(frame.payload, 0xa));
        continue;
      }
      if (frame.opcode !== 0x1 || authenticated) continue;
      authenticated = true;
      send(snapshotFrame(projectId, sandbox));
      ticker = setInterval(() => {
        const tick = tickFrame(projectId, sandbox, done);
        done = tick.done;
        if (tick.progress) send(tick.progress);
        if (tick.terminal && !terminalSent) {
          terminalSent = true;
          send(tick.terminal);
        }
        if (Date.now() - lastFrameAt >= 30_000) send({ kind: "heartbeat" });
      }, 400);
    }
  }
}

function encodeFrame(payload: Buffer, opcode = 0x1): Buffer {
  const first = 0x80 | opcode;
  if (payload.length < 126) return Buffer.concat([Buffer.from([first, payload.length]), payload]);
  if (payload.length < 65_536) {
    const head = Buffer.alloc(4);
    head[0] = first;
    head[1] = 126;
    head.writeUInt16BE(payload.length, 2);
    return Buffer.concat([head, payload]);
  }
  const head = Buffer.alloc(10);
  head[0] = first;
  head[1] = 127;
  head.writeBigUInt64BE(BigInt(payload.length), 2);
  return Buffer.concat([head, payload]);
}

function parseClientFrames(buffer: Buffer): {
  frames: { opcode: number; payload: Buffer }[];
  rest: Buffer;
} {
  const frames: { opcode: number; payload: Buffer }[] = [];
  let offset = 0;
  while (buffer.length - offset >= 2) {
    const opcode = buffer[offset] & 0x0f;
    const masked = (buffer[offset + 1] & 0x80) !== 0;
    let length = buffer[offset + 1] & 0x7f;
    let start = offset + 2;
    if (length === 126) {
      if (buffer.length - offset < 4) break;
      length = buffer.readUInt16BE(offset + 2);
      start = offset + 4;
    } else if (length === 127) {
      if (buffer.length - offset < 10) break;
      length = Number(buffer.readBigUInt64BE(offset + 2));
      start = offset + 10;
    }
    let maskKey: Buffer | null = null;
    if (masked) {
      if (buffer.length - offset < start - offset + 4) break;
      maskKey = buffer.subarray(start, start + 4);
      start += 4;
    }
    if (buffer.length < start + length) break;
    let payload = buffer.subarray(start, start + length);
    if (maskKey) {
      const unmasked = Buffer.alloc(length);
      for (let index = 0; index < length; index++)
        unmasked[index] = payload[index] ^ maskKey[index % 4];
      payload = unmasked;
    }
    frames.push({ opcode, payload });
    offset = start + length;
  }
  return { frames, rest: buffer.subarray(offset) };
}
