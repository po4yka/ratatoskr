# Ratatoskr Quick Save Extension

Manifest V3 browser extension for saving the current tab to Ratatoskr through `POST /v1/quick-save`.

## Supported Browsers

- Chrome / Chromium / Edge: load `extension/` as an unpacked extension.
- Firefox: load `extension/manifest.json` from `about:debugging#/runtime/this-firefox`.

## Local Install

1. Start the Ratatoskr API and make sure browser-extension auth is enabled with `JWT_SECRET_KEY` and `ALLOWED_CLIENT_IDS` containing `browser-extension`.
2. Open the browser extension developer page.
3. Load this `extension/` directory as an unpacked extension.
4. Click the toolbar button, set the API URL, and sign in with nickname/email credentials.
5. Open an article tab and click the toolbar button again. The popup quick-saves the current tab immediately; **Save Current Tab** remains available for edited titles, tags, and resubmits.

## Runtime Behavior

- Access tokens are stored in `chrome.storage.session` when available. Browsers without that API fall back to extension local storage.
- API URL, last identifier, and the offline queue are stored in `chrome.storage.local`.
- The popup captures the current tab URL/title and selected page text, then posts:

```json
{
  "url": "https://example.com/article",
  "title": "Article title",
  "selected_text": "Highlighted passage",
  "tag_names": ["reading", "research"],
  "summarize": true
}
```

- Failed saves are queued locally and retried from the popup and from a background alarm every five minutes.

## Packaging

Build a release zip from the repository root:

```bash
python tools/scripts/build_extension_zip.py
```

The artifact is written to `dist/ratatoskr-quick-save-extension.zip`.

## Screenshots

The extension is intentionally plain. These screenshots show the three states worth checking before store submission.

![Signed-out popup](screenshots/signed-out.svg)

![Signed-in popup](screenshots/signed-in.svg)

![Offline queue popup](screenshots/offline-queue.svg)
