const ext = globalThis.browser ?? globalThis.chrome;
const DEFAULT_API_BASE_URL = "http://127.0.0.1:18000";
const CLIENT_ID = "browser-extension";

const statusEl = document.getElementById("status");
const apiBaseUrlEl = document.getElementById("apiBaseUrl");
const loginForm = document.getElementById("loginForm");
const saveForm = document.getElementById("saveForm");
const logoutButton = document.getElementById("logoutButton");
const retryButton = document.getElementById("retryButton");
const queueCountEl = document.getElementById("queueCount");
const pageTitleEl = document.getElementById("pageTitle");

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

function sessionArea() {
  return ext.storage.session ? "session" : "local";
}

async function getAccessToken() {
  const data = await storageGet(sessionArea(), ["accessToken"]);
  return data.accessToken || "";
}

async function setAccessToken(token) {
  await storageSet(sessionArea(), { accessToken: token });
}

async function clearAccessToken() {
  await storageRemove(sessionArea(), ["accessToken"]);
}

function cleanApiBaseUrl(value) {
  return (value || DEFAULT_API_BASE_URL).trim().replace(/\/+$/, "");
}

function setStatus(message, kind = "") {
  statusEl.textContent = message;
  if (kind) statusEl.dataset.kind = kind;
  else delete statusEl.dataset.kind;
}

async function currentTab() {
  const tabs = await callApi(ext.tabs.query.bind(ext.tabs), { active: true, currentWindow: true });
  return tabs && tabs[0] ? tabs[0] : null;
}

async function selectedText(tabId) {
  if (!ext.scripting || !tabId) return "";
  try {
    const results = await callApi(ext.scripting.executeScript.bind(ext.scripting), {
      target: { tabId },
      func: () => String(globalThis.getSelection ? globalThis.getSelection() : "").slice(0, 8000)
    });
    return results && results[0] ? String(results[0].result || "") : "";
  } catch (_error) {
    return "";
  }
}

function parseTags(value) {
  return value
    .split(",")
    .map(tag => tag.trim())
    .filter(Boolean);
}

async function refreshQueueCount() {
  const { quickSaveQueue = [] } = await storageGet("local", ["quickSaveQueue"]);
  queueCountEl.textContent = String(quickSaveQueue.length);
}

async function updateAuthState() {
  const token = await getAccessToken();
  loginForm.hidden = Boolean(token);
  saveForm.hidden = !token;
  logoutButton.hidden = !token;
}

async function loadSettings() {
  const settings = await storageGet("local", ["apiBaseUrl", "identifier"]);
  apiBaseUrlEl.value = settings.apiBaseUrl || DEFAULT_API_BASE_URL;
  document.getElementById("identifier").value = settings.identifier || "";
  const tab = await currentTab();
  pageTitleEl.value = tab?.title || "";
}

async function login(event) {
  event.preventDefault();
  const apiBaseUrl = cleanApiBaseUrl(apiBaseUrlEl.value);
  const identifier = document.getElementById("identifier").value.trim();
  const password = document.getElementById("password").value;
  const rememberMe = document.getElementById("rememberMe").checked;
  if (!identifier || !password) {
    setStatus("Enter credentials.", "error");
    return;
  }
  setStatus("Signing in...");
  const response = await fetch(`${apiBaseUrl}/v1/auth/credentials-login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      identifier,
      password,
      remember_me: rememberMe,
      client_id: CLIENT_ID
    })
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.success === false) {
    throw new Error(payload.error?.message || payload.detail || "Sign-in failed");
  }
  const token = payload.data?.tokens?.accessToken || payload.data?.tokens?.access_token;
  if (!token) throw new Error("Sign-in response did not include an access token");
  await setAccessToken(token);
  await storageSet("local", { apiBaseUrl, identifier });
  document.getElementById("password").value = "";
  await updateAuthState();
  setStatus("Signed in.", "success");
}

async function queueSave(item) {
  const { quickSaveQueue = [] } = await storageGet("local", ["quickSaveQueue"]);
  quickSaveQueue.push({ ...item, queuedAt: new Date().toISOString() });
  await storageSet("local", { quickSaveQueue });
  await refreshQueueCount();
}

async function saveItem(item) {
  const token = await getAccessToken();
  if (!token) throw new Error("Sign in before saving");
  const apiBaseUrl = cleanApiBaseUrl(apiBaseUrlEl.value);
  await storageSet("local", { apiBaseUrl });
  const response = await fetch(`${apiBaseUrl}/v1/quick-save`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(item)
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.success === false) {
    throw new Error(payload.error?.message || payload.detail || `Save failed (${response.status})`);
  }
  return payload.data || {};
}

async function saveCurrentTab(event) {
  event?.preventDefault();
  const tab = await currentTab();
  if (!tab?.url || !/^https?:\/\//i.test(tab.url)) {
    setStatus("Open an http(s) page first.", "error");
    return;
  }
  const item = {
    url: tab.url,
    title: pageTitleEl.value.trim() || tab.title || null,
    selected_text: await selectedText(tab.id),
    tag_names: parseTags(document.getElementById("tags").value),
    summarize: document.getElementById("summarize").checked
  };
  setStatus("Saving...");
  try {
    const data = await saveItem(item);
    const label = data.duplicate ? "Already saved." : "Saved.";
    setStatus(`${label} Request ${data.request_id || data.requestId || ""}`.trim(), "success");
  } catch (error) {
    await queueSave(item);
    setStatus(`Queued for retry: ${error.message}`, "error");
  }
}

async function retryQueue() {
  const token = await getAccessToken();
  if (!token) {
    setStatus("Sign in before retrying.", "error");
    return;
  }
  const { quickSaveQueue = [] } = await storageGet("local", ["quickSaveQueue"]);
  if (!quickSaveQueue.length) {
    setStatus("Queue is empty.");
    return;
  }
  const remaining = [];
  for (const item of quickSaveQueue) {
    try {
      await saveItem(item);
    } catch (_error) {
      remaining.push(item);
    }
  }
  await storageSet("local", { quickSaveQueue: remaining });
  await refreshQueueCount();
  setStatus(remaining.length ? `${remaining.length} item(s) still queued.` : "Queued saves replayed.", remaining.length ? "error" : "success");
}

loginForm.addEventListener("submit", event => {
  login(event).catch(error => setStatus(error.message, "error"));
});
saveForm.addEventListener("submit", event => {
  saveCurrentTab(event).catch(error => setStatus(error.message, "error"));
});
retryButton.addEventListener("click", () => {
  retryQueue().catch(error => setStatus(error.message, "error"));
});
logoutButton.addEventListener("click", async () => {
  await clearAccessToken();
  await updateAuthState();
  setStatus("Signed out.");
});

document.addEventListener("DOMContentLoaded", async () => {
  await loadSettings();
  await updateAuthState();
  await refreshQueueCount();
  await retryQueue().catch(() => undefined);
  if (await getAccessToken()) await saveCurrentTab();
});
