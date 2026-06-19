const ext = globalThis.browser ?? globalThis.chrome;
const DEFAULT_API_BASE_URL = "http://127.0.0.1:18000";

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

function sessionArea() {
  return ext.storage.session ? "session" : "local";
}

async function accessToken() {
  const data = await storageGet(sessionArea(), ["accessToken"]);
  return data.accessToken || "";
}

async function replayQueue() {
  const token = await accessToken();
  if (!token) return;
  const { quickSaveQueue = [], apiBaseUrl = DEFAULT_API_BASE_URL } = await storageGet("local", [
    "quickSaveQueue",
    "apiBaseUrl"
  ]);
  if (!quickSaveQueue.length) return;
  const baseUrl = String(apiBaseUrl || DEFAULT_API_BASE_URL).replace(/\/+$/, "");
  const remaining = [];
  for (const item of quickSaveQueue) {
    try {
      const response = await fetch(`${baseUrl}/v1/quick-save`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify(item)
      });
      if (!response.ok) remaining.push(item);
    } catch (_error) {
      remaining.push(item);
    }
  }
  await storageSet("local", { quickSaveQueue: remaining });
}

ext.runtime.onInstalled.addListener(() => {
  ext.alarms.create("retryQuickSaveQueue", { periodInMinutes: 5 });
});

ext.runtime.onStartup.addListener(() => {
  replayQueue().catch(() => undefined);
});

ext.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "retryQuickSaveQueue") replayQueue().catch(() => undefined);
});
