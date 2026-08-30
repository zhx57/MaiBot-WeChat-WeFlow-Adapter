"use strict";

const path = require("path");
const { openEncryptedDatabase } = require("./lib/wechat-store");

async function readInput() {
  let value = "";
  process.stdin.setEncoding("utf8");
  for await (const chunk of process.stdin) {
    value += chunk;
    if (value.length > 16384) throw new Error("INPUT_TOO_LARGE");
  }
  return JSON.parse(value || "{}");
}

async function main() {
  const config = await readInput();
  const accountDir = path.resolve(String(config.accountDir || ""));
  const key = String(config.dbKeyHex || "");
  if (!/^[0-9a-f]{64}$/i.test(key)) throw new Error("INVALID_INPUT");
  const databasePath = path.join(accountDir, "db_storage", "contact", "contact.db");
  const db = openEncryptedDatabase(databasePath, Buffer.from(key, "hex"));
  try {
    const result = db.prepare(
      "SELECT count(*) AS table_count FROM sqlite_master WHERE type='table'",
    ).get();
    if (Number(result?.table_count || 0) < 1) throw new Error("NO_TABLES");
  } finally {
    db.close();
  }
  process.stdout.write('{"ok":true}');
}

if (require.main === module) main().catch(() => {
  process.stdout.write('{"ok":false,"errorCode":"DB_KEY_INVALID"}');
  process.exitCode = 1;
});

module.exports = { main };
