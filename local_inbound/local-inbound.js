"use strict";

const fs = require("fs");
const readline = require("readline");

const { InboundFilter } = require("./lib/inbound-filter");
const { WechatStore } = require("./lib/wechat-store");

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

let store = null;
let configured = false;
let stopping = false;
let scanning = false;
let scanTimer = null;
let scanTimerDueAt = 0;
let fullScanRequested = false;
let scanRequestedAtMs = 0;
let scanTrigger = "";
const requestedDatabaseNames = new Set();
let watcher = null;
let pollTimer = null;
let fallbackScanTimer = null;
let heartbeatTimer = null;
let metadataTimer = null;
let eventSequence = 0;
let activeWorkers = 0;
let dispatchWorkers = 8;
let inboundFilter = new InboundFilter();
let filteredEvents = 0;

const pendingBySession = new Map();
const activeSessions = new Set();
const acknowledgements = new Map();
const retryDelaysBySession = new Map();
const retryAfterBySession = new Map();

class InboundRejectedError extends Error {
  constructor(message) {
    super(message);
    this.name = "InboundRejectedError";
  }
}

function emit(value) {
  return new Promise((resolve, reject) => {
    const line = `${JSON.stringify(value)}\n`;
    process.stdout.write(line, (error) => (error ? reject(error) : resolve()));
  });
}

function safeError(error) {
  return String(error?.message || error || "unknown error").replace(/[\r\n]+/g, " ").slice(0, 1000);
}

function validateConfig(config) {
  if (!config || typeof config !== "object") throw new Error("Missing configuration object");
  if (!/^[0-9a-f]{64}$/i.test(String(config.dbKeyHex || ""))) throw new Error("Invalid database key");
  for (const key of ["accountDir", "wxid", "mediaDir", "statePath", "nativeImageAddonPath"]) {
    if (!String(config[key] || "").trim()) throw new Error(`Missing configuration field: ${key}`);
  }
}

function waitForAck(eventId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      acknowledgements.delete(eventId);
      reject(new Error(`Inbound acknowledgement timed out: ${eventId}`));
    }, timeoutMs);
    acknowledgements.set(eventId, {
      resolve(value) {
        clearTimeout(timeout);
        resolve(value);
      },
      reject(error) {
        clearTimeout(timeout);
        reject(error);
      },
    });
  });
}

async function deliverEvent(event) {
  const ack = event._ack;
  const timing = { ...(event._timing || {}) };
  const publicEvent = { ...event };
  delete publicEvent._ack;
  delete publicEvent._timing;
  // 媒体补全会删除解析用临时字段，因此只处理投递副本，保留原事件供 NACK 重试。
  const mediaStartedAt = performance.now();
  await store.enrichMedia(publicEvent);
  timing.mediaMs = performance.now() - mediaStartedAt;
  timing.dispatchQueueMs = Math.max(0, Date.now() - Number(timing.readyAtMs || Date.now()));
  timing.emittedAtMs = Date.now();
  const eventId = `local-${process.pid}-${++eventSequence}`;
  const ackPromise = waitForAck(eventId);
  await emit({ type: "event", eventId, event: publicEvent, timing });
  const result = await ackPromise;
  if (!result?.ok) {
    throw new InboundRejectedError(result?.error || "Inbound callback rejected event");
  }
  store.confirm(ack);
}

function runNextSessions() {
  if (stopping) return;
  while (activeWorkers < dispatchWorkers) {
    const candidate = [...pendingBySession.entries()].find(
      ([sessionId, queue]) =>
        queue.length &&
        !activeSessions.has(sessionId) &&
        Date.now() >= (retryAfterBySession.get(sessionId) || 0),
    );
    if (!candidate) break;
    const [sessionId] = candidate;
    activeSessions.add(sessionId);
    activeWorkers += 1;
    void drainSession(sessionId);
  }
}

async function drainSession(sessionId) {
  try {
    const queue = pendingBySession.get(sessionId) || [];
    while (queue.length && !stopping) {
      const event = queue[0];
      try {
        await deliverEvent(event);
      } catch (error) {
        if (!(error instanceof InboundRejectedError)) throw error;
        store.reject();
        const previousDelay = retryDelaysBySession.get(sessionId) || 0;
        const retryDelay = previousDelay ? Math.min(30000, previousDelay * 2) : 1000;
        retryDelaysBySession.set(sessionId, retryDelay);
        retryAfterBySession.set(sessionId, Date.now() + retryDelay);
        await emit({
          type: "warning",
          error:
            `Inbound callback rejected session ${sessionId}; ` +
            `retrying this session in ${retryDelay}ms: ${safeError(error)}`,
        }).catch(() => {});
        const retryTimer = setTimeout(() => runNextSessions(), retryDelay);
        retryTimer.unref();
        return;
      }
      queue.shift();
      retryDelaysBySession.delete(sessionId);
      retryAfterBySession.delete(sessionId);
    }
    if (!queue.length) {
      pendingBySession.delete(sessionId);
      retryDelaysBySession.delete(sessionId);
      retryAfterBySession.delete(sessionId);
    }
  } catch (error) {
    store.reject();
    await emit({ type: "fatal", error: safeError(error) }).catch(() => {});
    stopping = true;
    process.exitCode = 2;
    setImmediate(() => process.exit(2));
  } finally {
    activeSessions.delete(sessionId);
    activeWorkers = Math.max(0, activeWorkers - 1);
    runNextSessions();
  }
}

function enqueueGroups(groups) {
  for (const group of groups) {
    const queue = pendingBySession.get(group.sessionId) || [];
    for (const event of group.events) {
      if (inboundFilter.allows(event)) {
        queue.push(event);
      } else {
        // 被过滤的行也必须确认，否则该消息表的连续水位无法推进。
        store.confirm(event._ack);
        filteredEvents += 1;
      }
    }
    if (queue.length) pendingBySession.set(group.sessionId, queue);
  }
  runNextSessions();
}

function takeScanRequest() {
  const request = {
    databaseNames: fullScanRequested ? null : [...requestedDatabaseNames],
    requestedAtMs: scanRequestedAtMs || Date.now(),
    scanStartedAtMs: Date.now(),
    trigger: scanTrigger || "unknown",
  };
  fullScanRequested = false;
  scanRequestedAtMs = 0;
  scanTrigger = "";
  requestedDatabaseNames.clear();
  return request;
}

function hasPendingScan() {
  return fullScanRequested || requestedDatabaseNames.size > 0;
}

async function scan() {
  if (stopping || !store) return;
  if (scanning) return;
  scanning = true;
  const request = takeScanRequest();
  try {
    const groups = store.scanNewRows(request);
    enqueueGroups(groups);
  } catch (error) {
    await emit({ type: "warning", error: safeError(error) }).catch(() => {});
  } finally {
    scanning = false;
    if (hasPendingScan()) armScanTimer(10);
  }
}

function armScanTimer(delay) {
  if (stopping) return;
  const dueAt = Date.now() + Math.max(0, Number(delay) || 0);
  if (scanTimer && scanTimerDueAt <= dueAt) return;
  if (scanTimer) clearTimeout(scanTimer);
  scanTimerDueAt = dueAt;
  scanTimer = setTimeout(() => {
    scanTimer = null;
    scanTimerDueAt = 0;
    void scan();
  }, delay);
}

function scheduleScan(delay = 20, { databaseName = "", trigger = "manual" } = {}) {
  if (stopping) return;
  if (!scanRequestedAtMs) scanRequestedAtMs = Date.now();
  if (databaseName && !fullScanRequested) requestedDatabaseNames.add(String(databaseName).toLowerCase());
  else fullScanRequested = true;
  if (!scanTrigger) scanTrigger = trigger;
  else if (!scanTrigger.split("+").includes(trigger)) scanTrigger += `+${trigger}`;
  armScanTimer(delay);
}

async function configure(config) {
  validateConfig(config);
  dispatchWorkers = Math.max(1, Math.min(32, Number(config.dispatchWorkers || 8)));
  inboundFilter = new InboundFilter(config);
  store = new WechatStore(config);
  store.initialize((session) => inboundFilter.allows(session));
  watcher = fs.watch(store.messageDir, { persistent: false }, (eventType, filename) => {
    const match = /^(message_\d+\.db)(?:-(?:wal|shm))?$/i.exec(String(filename || ""));
    if (!match) {
      scheduleScan(5, { trigger: "watch-unknown" });
      return;
    }
    // rename 可能表示新增/替换数据库；普通 change/WAL 只需扫描对应分库。
    if (eventType === "rename" && /^message_\d+\.db$/i.test(String(filename || ""))) {
      store.markStructureDirty();
    }
    scheduleScan(5, { databaseName: match[1], trigger: "watch" });
  });
  pollTimer = setInterval(() => {
    try {
      const changedDatabaseNames = store.detectChangedMessageDatabases();
      for (const databaseName of changedDatabaseNames) {
        scheduleScan(0, { databaseName, trigger: "poll-change" });
      }
    } catch (error) {
      void emit({ type: "warning", error: `Message change detection failed: ${safeError(error)}` }).catch(() => {});
      scheduleScan(0, { trigger: "poll-error" });
    }
  }, Math.max(100, Number(config.pollIntervalMs || 250)));
  pollTimer.unref();
  fallbackScanTimer = setInterval(
    () => scheduleScan(0, { trigger: "fallback-full" }),
    Math.max(5000, Number(config.structureRefreshIntervalMs || 5000)),
  );
  fallbackScanTimer.unref();
  heartbeatTimer = setInterval(() => {
    void emit({
      type: "heartbeat",
      timestamp: Date.now(),
      activeWorkers,
      pendingSessions: pendingBySession.size,
      filteredEvents,
    }).catch(() => {});
  }, 10000);
  heartbeatTimer.unref();
  metadataTimer = setInterval(() => {
    try {
      store.refreshMetadata();
      void emit({
        type: "account_profile",
        accountProfile: store.selfProfile(),
      }).catch(() => {});
    } catch (error) {
      void emit({ type: "warning", error: `Metadata refresh failed: ${safeError(error)}` }).catch(() => {});
    }
  }, 5 * 60 * 1000);
  metadataTimer.unref();
  configured = true;
  await emit({
    type: "ready",
    backend: "local-database",
    sourceBaseline: "local-database-message-mapping",
    sessions: store.sessionMap.size,
    messageTables: store.tableMap.size,
    allowlistEntries: inboundFilter.allowlist.size,
    blocklistEntries: inboundFilter.blocklist.size,
    accountProfile: store.selfProfile(),
  });
  scheduleScan(0, { trigger: "startup" });
}

input.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    if (configured) void emit({ type: "warning", error: "Ignored invalid control JSON" }).catch(() => {});
    return;
  }
  if (!configured) {
    void configure(message).catch(async (error) => {
      await emit({ type: "fatal", error: safeError(error) }).catch(() => {});
      process.exit(2);
    });
    return;
  }
  if (message?.type === "ack" || message?.type === "nack") {
    const pending = acknowledgements.get(String(message.eventId || ""));
    if (!pending) return;
    acknowledgements.delete(String(message.eventId));
    pending.resolve({ ok: message.type === "ack", error: String(message.error || "") });
  } else if (message?.type === "scan") {
    scheduleScan(0, { trigger: "control" });
  } else if (message?.type === "stop") {
    void shutdown(0);
  }
});

async function shutdown(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  if (scanTimer) clearTimeout(scanTimer);
  if (pollTimer) clearInterval(pollTimer);
  if (fallbackScanTimer) clearInterval(fallbackScanTimer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (metadataTimer) clearInterval(metadataTimer);
  watcher?.close();
  try {
    store?.close();
  } catch {
    exitCode = exitCode || 1;
  }
  process.exit(exitCode);
}

process.on("SIGINT", () => void shutdown(0));
process.on("SIGTERM", () => void shutdown(0));
process.on("uncaughtException", async (error) => {
  await emit({ type: "fatal", error: safeError(error) }).catch(() => {});
  process.exit(2);
});
process.on("unhandledRejection", async (error) => {
  await emit({ type: "fatal", error: safeError(error) }).catch(() => {});
  process.exit(2);
});
