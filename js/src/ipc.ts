import { decode, encode } from "@msgpack/msgpack";
import { createConnection } from "node:net";
import { generateUlid } from "./ulid.js";

export class ShakeClientError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ShakeClientError";
  }
}

export class DaemonNotRunningError extends ShakeClientError {
  constructor(message: string) {
    super(message);
    this.name = "DaemonNotRunningError";
  }
}

export interface IpcResponse {
  id: string;
  data: unknown;
}

function packFrame(payload: Uint8Array): Buffer {
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payload.length, 0);
  return Buffer.concat([header, Buffer.from(payload)]);
}

async function readExact(socket: import("node:net").Socket, n: number): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let got = 0;
  while (got < n) {
    const chunk = socket.read(n - got) as Buffer | null;
    if (chunk) {
      chunks.push(chunk);
      got += chunk.length;
      continue;
    }
    await new Promise<void>((resolve, reject) => {
      const onReadable = () => {
        cleanup();
        resolve();
      };
      const onError = (err: Error) => {
        cleanup();
        reject(err);
      };
      const onClose = () => {
        cleanup();
        reject(new ConnectionError("socket closed"));
      };
      const cleanup = () => {
        socket.off("readable", onReadable);
        socket.off("error", onError);
        socket.off("close", onClose);
      };
      socket.once("readable", onReadable);
      socket.once("error", onError);
      socket.once("close", onClose);
    });
  }
  return Buffer.concat(chunks);
}

class ConnectionError extends Error {}

export async function sendIpc(
  socketPath: string,
  action: string,
  params: Record<string, unknown> = {},
  timeoutMs = 10_000,
): Promise<IpcResponse> {
  const payload = encode({
    id: generateUlid(),
    action,
    params,
  });

  return await new Promise<IpcResponse>((resolve, reject) => {
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new DaemonNotRunningError(`IPC timeout after ${timeoutMs}ms (${action})`));
    }, timeoutMs);

    const socket = createConnection({ path: socketPath });

    const fail = (err: unknown) => {
      clearTimeout(timer);
      socket.destroy();
      const message = err instanceof Error ? err.message : String(err);
      reject(
        new DaemonNotRunningError(`Cannot connect to shaked at ${socketPath}: ${message}`),
      );
    };

    socket.once("error", fail);

    socket.once("connect", () => {
      socket.write(packFrame(payload), (writeErr) => {
        if (writeErr) fail(writeErr);
      });
    });

    socket.once("readable", () => {
      void (async () => {
        try {
          const header = await readExact(socket, 4);
          const length = header.readUInt32BE(0);
          const body = await readExact(socket, length);
          const raw = decode(body) as {
            id: string;
            ok: boolean;
            data?: unknown;
            error?: string | null;
          };
          clearTimeout(timer);
          socket.end();
          if (!raw.ok) {
            reject(new ShakeClientError(raw.error || "Unknown daemon error"));
            return;
          }
          resolve({ id: String(raw.id), data: raw.data });
        } catch (err) {
          fail(err);
        }
      })();
    });
  });
}
