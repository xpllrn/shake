import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { resolvePaths, type ShakePaths } from "./config.js";
import { DaemonNotRunningError, sendIpc } from "./ipc.js";

const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

function findRepoRoot(): string | null {
  let dir = packageRoot;
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(dir, "pyproject.toml")) && existsSync(join(dir, "src", "shake"))) {
      return dir;
    }
    dir = join(dir, "..");
  }
  return null;
}

function pythonPath(): string {
  const repo = findRepoRoot();
  if (repo) return join(repo, "src");
  return join(packageRoot, "python");
}

export async function ping(paths: ShakePaths, timeoutMs = 1000): Promise<boolean> {
  if (!existsSync(paths.socketPath)) return false;
  try {
    const res = await sendIpc(paths.socketPath, "ping", {}, timeoutMs);
    return res.data === "pong";
  } catch {
    return false;
  }
}

export interface StartDaemonOptions {
  home?: string;
  python?: string;
}

export async function startDaemon(
  options: StartDaemonOptions = {},
): Promise<{ paths: ShakePaths; child: ChildProcess | null }> {
  const paths = resolvePaths(options.home);
  mkdirSync(paths.home, { recursive: true, mode: 0o700 });
  mkdirSync(paths.keysDir, { recursive: true, mode: 0o700 });

  if (await ping(paths)) {
    return { paths, child: null };
  }

  const repo = findRepoRoot();
  const env = {
    ...process.env,
    SHAKE_HOME: paths.home,
    PYTHONPATH: [pythonPath(), process.env.PYTHONPATH].filter(Boolean).join(":"),
  };

  const python = options.python || process.env.SHAKE_PYTHON || "python3";
  const useUv = Boolean(repo && existsSync(join(repo, "pyproject.toml")));

  const child = useUv
    ? spawn("uv", ["run", "python", "-m", "shake.daemon"], {
        cwd: repo!,
        env,
        stdio: "ignore",
        detached: true,
      })
    : spawn(python, ["-m", "shake.daemon"], {
        env,
        stdio: "ignore",
        detached: true,
      });

  child.unref();

  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    if (await ping(paths, 400)) {
      return { paths, child };
    }
    await new Promise((r) => setTimeout(r, 100));
  }

  child.kill();
  throw new DaemonNotRunningError(
    `shaked failed to start (socket ${paths.socketPath}). Need Python 3.12+ with shake installed, or run from the shake repo with uv.`,
  );
}

export async function stopDaemon(paths: ShakePaths): Promise<void> {
  if (!(await ping(paths))) return;
  try {
    await sendIpc(paths.socketPath, "stop", {}, 3000);
  } catch {
    // socket going away is the success path
  }
}
