import { cpSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = join(root, "src", "shake");
const dest = join(dirname(fileURLToPath(import.meta.url)), "..", "python", "shake");

if (!existsSync(src)) {
  console.warn("shake python source not found; skip bundle:", src);
  process.exit(0);
}

rmSync(join(dest, ".."), { recursive: true, force: true });
mkdirSync(join(dest, ".."), { recursive: true });
cpSync(src, dest, { recursive: true });
console.log("bundled python package ->", dest);
