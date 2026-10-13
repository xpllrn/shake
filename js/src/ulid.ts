const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

export function generateUlid(timestampMs: number = Date.now()): string {
  const timeBytes = Buffer.alloc(6);
  timeBytes.writeUIntBE(timestampMs, 0, 6);
  const randomBytes = crypto.getRandomValues(new Uint8Array(10));
  const full = Buffer.concat([timeBytes, Buffer.from(randomBytes)]);
  let val = BigInt("0x" + full.toString("hex"));
  const chars: string[] = [];
  for (let i = 0; i < 26; i++) {
    chars.push(CROCKFORD[Number(val & 0x1fn)]);
    val >>= 5n;
  }
  return chars.reverse().join("");
}
