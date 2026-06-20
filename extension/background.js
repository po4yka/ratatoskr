const ext = globalThis.browser ?? globalThis.chrome;
const DEFAULT_API_BASE_URL = "http://127.0.0.1:18000";
const CLIENT_ID = "browser-extension";
const MAX_QUEUE_ITEMS = 50;
const MAX_QUEUE_AGE_MS = 7 * 24 * 60 * 60 * 1000;

function callApi(fn, ...args) {
  return new Promise((resolve, reject) => {
    try {
      const result = fn(...args, value => {
        const err = ext.runtime.lastError;
        if (err) reject(new Error(err.message));
        else resolve(value);
      });
      if (result && typeof result.then === "function") result.then(resolve, reject);
    } catch (error) {
      reject(error);
    }
  });
}

async function storageGet(area, keys) {
  return callApi(ext.storage[area].get.bind(ext.storage[area]), keys);
}

async function storageSet(area, value) {
  return callApi(ext.storage[area].set.bind(ext.storage[area]), value);
}

async function storageRemove(area, keys) {
  return callApi(ext.storage[area].remove.bind(ext.storage[area]), keys);
}

async function authStorageArea() {
  const data = await storageGet("local", ["authStorageArea"]);
  if (data.authStorageArea === "local") return "local";
  return ext.storage.session ? "session" : "local";
}

async function getTokens() {
  const area = await authStorageArea();
  const data = await storageGet(area, ["accessToken", "refreshToken"]);
  return { area, accessToken: data.accessToken || "", refreshToken: data.refreshToken || "" };
}

async function setTokens(tokens, area) {
  await storageSet(area, {
    accessToken: tokens.accessToken,
    refreshToken: tokens.refreshToken
  });
  await storageSet("local", { authStorageArea: area });
}

async function clearTokens() {
  await storageRemove("local", ["accessToken", "refreshToken", "authStorageArea"]);
  if (ext.storage.session) await storageRemove("session", ["accessToken", "refreshToken"]);
}

function isLoopbackHost(hostname) {
  const host = hostname.toLowerCase();
  return host === "localhost" || host === "127.0.0.1" || host === "::1" || host.endsWith(".localhost");
}

function cleanApiBaseUrl(value) {
  const raw = String(value || DEFAULT_API_BASE_URL).trim().replace(/\/+$/, "");
  const parsed = new URL(raw);
  // Reject non-HTTPS for remote hosts: tokens must not be sent over plain HTTP
  // to a non-loopback address. HTTP is allowed only for local development.
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isLoopbackHost(parsed.hostname))) {
    throw new Error("Use HTTPS for remote API URLs. Plain HTTP is allowed only for localhost.");
  }
  return parsed.origin;
}

function tokenPair(payload) {
  const tokens = payload.data?.tokens || {};
  return {
    accessToken: tokens.accessToken || tokens.access_token || "",
    refreshToken: tokens.refreshToken || tokens.refresh_token || ""
  };
}

async function refreshAccessToken(baseUrl) {
  const tokens = await getTokens();
  if (!tokens.refreshToken) return "";
  const response = await fetch(`${baseUrl}/v1/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      refresh_token: tokens.refreshToken,
      client_id: CLIENT_ID
    })
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.success === false) {
    await clearTokens();
    return "";
  }
  const refreshed = tokenPair(payload);
  if (!refreshed.accessToken || !refreshed.refreshToken) {
    await clearTokens();
    return "";
  }
  await setTokens(refreshed, tokens.area);
  return refreshed.accessToken;
}

function queueEntries(items) {
  const cutoff = Date.now() - MAX_QUEUE_AGE_MS;
  return items
    .filter(entry => {
      const queuedAt = Date.parse(entry.queuedAt || "");
      return entry.payload?.url && (!Number.isFinite(queuedAt) || queuedAt >= cutoff);
    })
    .slice(-MAX_QUEUE_ITEMS);
}

async function replayQueue() {
  let tokens = await getTokens();
  if (!tokens.accessToken) return;
  const { quickSaveQueue = [], apiBaseUrl = DEFAULT_API_BASE_URL } = await storageGet("local", [
    "quickSaveQueue",
    "apiBaseUrl"
  ]);
  const entries = queueEntries(quickSaveQueue);
  if (!entries.length) return;
  let baseUrl;
  try {
    baseUrl = cleanApiBaseUrl(apiBaseUrl);
  } catch (_error) {
    // Stored URL is invalid or non-HTTPS; skip replay until the user corrects it.
    return;
  }
  const remaining = [];
  for (const entry of entries) {
    let response;
    try {
      response = await fetch(`${baseUrl}/v1/quick-save`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${tokens.accessToken}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify(entry.payload)
      });
      if (response.status === 401) {
        const accessToken = await refreshAccessToken(baseUrl);
        if (!accessToken) {
          // Token refresh failed (session expired). Preserve this entry and all
          // subsequent unprocessed entries so the next alarm cycle can retry
          // them after the user signs in again. Without slicing the tail here,
          // entries after the current one would be silently dropped.
          const currentIndex = entries.indexOf(entry);
          for (const unprocessed of entries.slice(currentIndex)) {
            remaining.push(unprocessed);
          }
          break;
        }
        tokens = { ...tokens, accessToken };
        response = await fetch(`${baseUrl}/v1/quick-save`, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${tokens.accessToken}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify(entry.payload)
        });
      }
      if (response.status === 429 || response.status >= 500) {
        remaining.push({ ...entry, attempts: Number(entry.attempts || 0) + 1 });
      }
    } catch (_error) {
      remaining.push({ ...entry, attempts: Number(entry.attempts || 0) + 1 });
    }
  }
  await storageSet("local", { quickSaveQueue: queueEntries(remaining) });
}

async function ensureRetryAlarm() {
  if (!ext.alarms) return;
  const alarm = await callApi(ext.alarms.get.bind(ext.alarms), "retryQuickSaveQueue").catch(() => null);
  if (!alarm) ext.alarms.create("retryQuickSaveQueue", { periodInMinutes: 5 });
}

ext.runtime.onInstalled.addListener(() => {
  ensureRetryAlarm().catch(() => undefined);
});

ext.runtime.onStartup.addListener(() => {
  ensureRetryAlarm().catch(() => undefined);
  replayQueue().catch(() => undefined);
});

ext.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "retryQuickSaveQueue") replayQueue().catch(() => undefined);
});

ensureRetryAlarm().catch(() => undefined);
