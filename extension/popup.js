const ext = globalThis.browser ?? globalThis.chrome;
const DEFAULT_API_BASE_URL = "http://127.0.0.1:18000";
const CLIENT_ID = "browser-extension";
const MAX_QUEUE_ITEMS = 50;
const MAX_QUEUE_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_TAG_LENGTH = 80;
const SELECTED_TEXT_LIMIT = 8000;

const statusEl = document.getElementById("status");
const apiBaseUrlEl = document.getElementById("apiBaseUrl");
const loginForm = document.getElementById("loginForm");
const saveForm = document.getElementById("saveForm");
const logoutButton = document.getElementById("logoutButton");
const retryButton = document.getElementById("retryButton");
const queueCountEl = document.getElementById("queueCount");
const pageTitleEl = document.getElementById("pageTitle");
const includeSelectionEl = document.getElementById("includeSelection");
const selectedTextPreviewEl = document.getElementById("selectedTextPreview");

class ApiError extends Error {
  constructor(message, { status = 0, retryable = false, authExpired = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryable = retryable;
    this.authExpired = authExpired;
  }
}

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

async function setTokens(tokens, { rememberMe = false } = {}) {
  const area = rememberMe || !ext.storage.session ? "local" : "session";
  await storageSet(area, {
    accessToken: tokens.accessToken,
    refreshToken: tokens.refreshToken
  });
  await storageSet("local", { authStorageArea: area });
  if (area !== "local") await storageRemove("local", ["accessToken", "refreshToken"]);
  if (area !== "session" && ext.storage.session) {
    await storageRemove("session", ["accessToken", "refreshToken"]);
  }
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
  const raw = (value || DEFAULT_API_BASE_URL).trim().replace(/\/+$/, "");
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (_error) {
    throw new Error("Enter a valid API URL.");
  }
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isLoopbackHost(parsed.hostname))) {
    throw new Error("Use HTTPS for remote API URLs. Plain HTTP is allowed only for localhost.");
  }
  return parsed.origin;
}

async function ensureApiOriginPermission(apiBaseUrl) {
  if (!ext.permissions?.contains || !ext.permissions?.request) return;
  const parsed = new URL(apiBaseUrl);
  const origin = `${parsed.protocol}//${parsed.hostname}/*`;
  const permission = { origins: [origin] };
  if (await callApi(ext.permissions.contains.bind(ext.permissions), permission)) return;
  const granted = await callApi(ext.permissions.request.bind(ext.permissions), permission);
  if (!granted) throw new Error(`Grant extension access to ${parsed.origin} first.`);
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
      func: limit => String(globalThis.getSelection ? globalThis.getSelection() : "").slice(0, limit),
      args: [SELECTED_TEXT_LIMIT]
    });
    return results && results[0] ? String(results[0].result || "") : "";
  } catch (_error) {
    return "";
  }
}

function parseTags(value) {
  const tags = value
    .split(",")
    .map(tag => tag.trim())
    .filter(Boolean);
  for (const tag of tags) {
    if (tag.length > MAX_TAG_LENGTH) throw new Error(`Tag is too long: ${tag}`);
    if (tag.startsWith("#") || tag.startsWith("@")) {
      throw new Error(`Tag must not start with # or @: ${tag}`);
    }
  }
  return tags;
}

async function queueEntries() {
  const { quickSaveQueue = [] } = await storageGet("local", ["quickSaveQueue"]);
  const cutoff = Date.now() - MAX_QUEUE_AGE_MS;
  return quickSaveQueue.filter(entry => {
    const queuedAt = Date.parse(entry.queuedAt || "");
    return entry.payload?.url && (!Number.isFinite(queuedAt) || queuedAt >= cutoff);
  });
}

async function setQueueEntries(entries) {
  await storageSet("local", { quickSaveQueue: entries.slice(-MAX_QUEUE_ITEMS) });
  await refreshQueueCount();
}

async function refreshQueueCount() {
  const entries = await queueEntries();
  queueCountEl.textContent = String(entries.length);
  await storageSet("local", { quickSaveQueue: entries });
}

async function updateAuthState() {
  const tokens = await getTokens();
  loginForm.hidden = Boolean(tokens.accessToken);
  saveForm.hidden = !tokens.accessToken;
  logoutButton.hidden = !tokens.accessToken;
}

async function loadSettings() {
  const settings = await storageGet("local", ["apiBaseUrl", "identifier"]);
  apiBaseUrlEl.value = settings.apiBaseUrl || DEFAULT_API_BASE_URL;
  document.getElementById("identifier").value = settings.identifier || "";
  const tab = await currentTab();
  pageTitleEl.value = tab?.title || "";
  const preview = await selectedText(tab?.id);
  selectedTextPreviewEl.value = preview;
  includeSelectionEl.disabled = !preview;
}

function responseMessage(payload, fallback) {
  return payload.error?.message || payload.detail || fallback;
}

function tokenPair(payload) {
  const tokens = payload.data?.tokens || {};
  return {
    accessToken: tokens.accessToken || tokens.access_token || "",
    refreshToken: tokens.refreshToken || tokens.refresh_token || ""
  };
}

async function login(event) {
  event.preventDefault();
  const apiBaseUrl = cleanApiBaseUrl(apiBaseUrlEl.value);
  await ensureApiOriginPermission(apiBaseUrl);
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
    throw new Error(responseMessage(payload, "Sign-in failed"));
  }
  const tokens = tokenPair(payload);
  if (!tokens.accessToken || !tokens.refreshToken) {
    throw new Error("Sign-in response did not include access and refresh tokens");
  }
  await setTokens(tokens, { rememberMe });
  await storageSet("local", { apiBaseUrl, identifier });
  document.getElementById("password").value = "";
  await updateAuthState();
  setStatus("Signed in.", "success");
}

async function refreshAccessToken(apiBaseUrl) {
  const tokens = await getTokens();
  if (!tokens.refreshToken) throw new ApiError("Sign in again.", { status: 401, authExpired: true });
  const response = await fetch(`${apiBaseUrl}/v1/auth/refresh`, {
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
    throw new ApiError(responseMessage(payload, "Session expired. Sign in again."), {
      status: response.status,
      authExpired: true
    });
  }
  const refreshed = tokenPair(payload);
  if (!refreshed.accessToken || !refreshed.refreshToken) {
    await clearTokens();
    throw new ApiError("Refresh response did not include access and refresh tokens", {
      status: response.status,
      authExpired: true
    });
  }
  await setTokens(refreshed, { rememberMe: tokens.area === "local" });
  return refreshed.accessToken;
}

function classifyHttpFailure(status) {
  if (status === 401) return { retryable: false, authExpired: true };
  if (status === 429 || status >= 500) return { retryable: true, authExpired: false };
  return { retryable: false, authExpired: false };
}

async function queueSave(payload) {
  const entries = await queueEntries();
  const filtered = entries.filter(entry => entry.payload.url !== payload.url);
  filtered.push({ payload, queuedAt: new Date().toISOString(), attempts: 0 });
  await setQueueEntries(filtered);
}

async function saveItem(payload, { allowRefresh = true } = {}) {
  const tokens = await getTokens();
  if (!tokens.accessToken) throw new ApiError("Sign in before saving", { status: 401 });
  const apiBaseUrl = cleanApiBaseUrl(apiBaseUrlEl.value);
  await ensureApiOriginPermission(apiBaseUrl);
  await storageSet("local", { apiBaseUrl });
  let response;
  try {
    response = await fetch(`${apiBaseUrl}/v1/quick-save`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${tokens.accessToken}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(payload)
    });
  } catch (error) {
    throw new ApiError(error.message || "Network error", { retryable: true });
  }
  const body = await response.json().catch(() => ({}));
  if (response.status === 401 && allowRefresh) {
    await refreshAccessToken(apiBaseUrl);
    return saveItem(payload, { allowRefresh: false });
  }
  if (!response.ok || body.success === false) {
    const failure = classifyHttpFailure(response.status);
    throw new ApiError(responseMessage(body, `Save failed (${response.status})`), {
      status: response.status,
      retryable: failure.retryable,
      authExpired: failure.authExpired
    });
  }
  return body.data || {};
}

async function saveCurrentTab(event) {
  event?.preventDefault();
  let item;
  try {
    const tab = await currentTab();
    if (!tab?.url || !/^https?:\/\//i.test(tab.url)) {
      setStatus("Open an http(s) page first.", "error");
      return;
    }
    item = {
      url: tab.url,
      title: pageTitleEl.value.trim() || tab.title || null,
      selected_text: includeSelectionEl.checked ? selectedTextPreviewEl.value.slice(0, SELECTED_TEXT_LIMIT) : "",
      tag_names: parseTags(document.getElementById("tags").value),
      summarize: document.getElementById("summarize").checked
    };
  } catch (error) {
    setStatus(error.message, "error");
    return;
  }
  setStatus("Saving...");
  try {
    const data = await saveItem(item);
    const label = data.duplicate ? "Already saved." : "Saved.";
    setStatus(`${label} Request ${data.request_id || data.requestId || ""}`.trim(), "success");
  } catch (error) {
    if (error.authExpired) {
      await updateAuthState();
      setStatus(error.message, "error");
    } else if (error.retryable) {
      await queueSave(item);
      setStatus(`Queued for retry: ${error.message}`, "error");
    } else {
      setStatus(error.message, "error");
    }
  }
}

async function retryQueue() {
  const tokens = await getTokens();
  if (!tokens.accessToken) {
    setStatus("Sign in before retrying.", "error");
    return;
  }
  const entries = await queueEntries();
  if (!entries.length) {
    setStatus("Queue is empty.");
    return;
  }
  const remaining = [];
  for (const entry of entries) {
    try {
      await saveItem(entry.payload);
    } catch (error) {
      if (error.authExpired) {
        await updateAuthState();
        remaining.push(entry);
        setStatus(error.message, "error");
        break;
      }
      if (error.retryable) remaining.push({ ...entry, attempts: Number(entry.attempts || 0) + 1 });
    }
  }
  await setQueueEntries(remaining);
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
  const tokens = await getTokens();
  try {
    const apiBaseUrl = cleanApiBaseUrl(apiBaseUrlEl.value);
    if (tokens.accessToken && tokens.refreshToken) {
      await fetch(`${apiBaseUrl}/v1/auth/logout`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${tokens.accessToken}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ refresh_token: tokens.refreshToken })
      }).catch(() => undefined);
    }
  } catch (_error) {
    // Local token cleanup should still happen when the saved API URL is stale.
  }
  await clearTokens();
  await updateAuthState();
  setStatus("Signed out.");
});

document.addEventListener("DOMContentLoaded", async () => {
  await loadSettings();
  await updateAuthState();
  await refreshQueueCount();
  await retryQueue().catch(() => undefined);
});
