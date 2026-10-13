import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";

export interface ShakePaths {
  home: string;
  socketPath: string;
  dbPath: string;
  pidPath: string;
  logPath: string;
  keysDir: string;
}

export function resolveHome(home?: string): string {
  if (home) return home;
  if (process.env.SHAKE_HOME) return process.env.SHAKE_HOME;
  return join(homedir(), ".config", "shake");
}

export function resolvePaths(home?: string): ShakePaths {
  const resolved = resolveHome(home);
  const defaultSocket = join(resolved, "shaked.sock");
  let socketPath = defaultSocket;
  if (defaultSocket.length > 95) {
    const shortHash = createHash("sha256").update(resolved).digest("hex").slice(0, 8);
    socketPath = `/tmp/shaked_${shortHash}.sock`;
  }
  return {
    home: resolved,
    socketPath,
    dbPath: join(resolved, "shake.db"),
    pidPath: join(resolved, "shaked.pid"),
    logPath: join(resolved, "shaked.log"),
    keysDir: join(resolved, "keys"),
  };
}
