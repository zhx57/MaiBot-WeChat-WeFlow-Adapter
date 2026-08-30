"use strict";

// WebUI 只读通讯录 helper。数据库密钥通过 stdin 注入，不出现在命令行或日志中。
const path = require("path");
const { openEncryptedDatabase } = require("./lib/wechat-store");
const {
  classifySelectableContact,
  contactIdentifier,
} = require("./lib/contact-classification");

function quoteIdentifier(value) {
  return `"${String(value).replaceAll('"', '""')}"`;
}

function asText(value) {
  return String(value ?? "").trim();
}

function displayName(row, identifier) {
  for (const field of ["remark", "nick_name", "nickname", "alias", "name", "display_name"]) {
    const value = asText(row?.[field]);
    if (value) return value;
  }
  return identifier;
}

function addConversation(result, row) {
  const identifier = contactIdentifier(row);
  if (!identifier) return;
  const type = classifySelectableContact(row);
  if (!type) return;
  const current = result.get(identifier.toLowerCase());
  const candidate = {
    id: identifier,
    name: displayName(row, identifier),
    type,
    updated: Number(row?.updated || row?.update_time || row?.last_update_time || 0) || 0,
  };
  if (!current || candidate.name !== candidate.id || candidate.updated >= current.updated) {
    result.set(identifier.toLowerCase(), candidate);
  }
}

function readTableName(db, expectedName) {
  return db
    .prepare("SELECT name FROM sqlite_master WHERE type='table' AND lower(name)=? LIMIT 1")
    .get(expectedName.toLowerCase())?.name;
}

async function main() {
  const input = await new Promise((resolve, reject) => {
    let value = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      value += chunk;
    });
    process.stdin.on("end", () => resolve(value));
    process.stdin.on("error", reject);
  });
  const config = JSON.parse(input || "{}");
  const accountDir = path.resolve(asText(config.accountDir));
  const dbKeyHex = asText(config.dbKeyHex);
  if (!accountDir || !/^[0-9a-f]{64}$/i.test(dbKeyHex)) {
    throw new Error("通讯录读取凭据无效");
  }

  const result = new Map();
  const dbStorageDir = path.join(accountDir, "db_storage");
  const contactPath = path.join(dbStorageDir, "contact", "contact.db");
  const contactDb = openEncryptedDatabase(contactPath, Buffer.from(dbKeyHex, "hex"));
  try {
    const tableName = readTableName(contactDb, "contact");
    if (tableName) {
      const rows = contactDb
        .prepare(
          `SELECT username,alias,remark,nick_name,big_head_url,small_head_url,` +
            `local_type,flag,delete_flag,verify_flag,is_in_chat_room ` +
            `FROM ${quoteIdentifier(tableName)} WHERE username IS NOT NULL AND username != ''`,
        )
        .all();
      for (const row of rows) addConversation(result, row);
    }
  } finally {
    contactDb.close();
  }

  const conversations = [...result.values()].sort((left, right) => {
    const typeOrder = { group: 0, private: 1 };
    return (
      (typeOrder[left.type] ?? 9) - (typeOrder[right.type] ?? 9) ||
      left.name.localeCompare(right.name, "zh-CN") ||
      left.id.localeCompare(right.id)
    );
  });
  process.stdout.write(JSON.stringify({ conversations }));
}

if (require.main === module) main().catch((error) => {
  process.stderr.write(`${String(error?.message || error).replace(/[\r\n]+/g, " ").slice(0, 500)}\n`);
  process.stdout.write(JSON.stringify({ error: "通讯录读取失败" }));
  process.exitCode = 1;
});

module.exports = { addConversation, displayName };
