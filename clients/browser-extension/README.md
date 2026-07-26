# Ratatoskr Browser Extension

Save and summarize web pages with one click from Chrome or Firefox.

## Getting Started

### 1. Create API Credentials (Server Side)

Before configuring the extension, you need API credentials from your Ratatoskr server.

#### Option A: Secret-Key Auth (Recommended)

Requires `SECRET_LOGIN_ENABLED=true` on the server.

1. As the server owner, create a client secret via the API:

   ```bash
   curl -X POST https://ratatoskr.example.com/v1/auth/secret-keys \
     -H "Authorization: Bearer <owner-jwt>" \
     -H "Content-Type: application/json" \
     -d '{"label": "Browser Extension", "description": "Chrome/Firefox extension"}'
   ```

2. Save the returned `client_id` and `secret` -- the secret is shown only once.

3. Exchange the secret for a JWT token:

   ```bash
   curl -X POST https://ratatoskr.example.com/v1/auth/secret-login \
     -H "Content-Type: application/json" \
     -d '{"user_id": <your_telegram_user_id>, "client_id": "<client_id>", "secret": "<secret>"}'
   ```

4. Use the returned `access_token` as the API key in the extension settings.

#### Option B: Direct JWT Token

If you already have a JWT token (from the web UI session or Telegram WebApp), you can use it directly as the API key. Note that JWT tokens expire -- secret-key auth is preferred for long-lived extension use.

### 2. Install the Extension

#### Chrome

1. Open `chrome://extensions/`
2. Enable "Developer mode" (top right toggle)
3. Click "Load unpacked"
4. Select this `clients/browser-extension/` directory
5. The extension icon appears in the toolbar

#### Firefox

1. Open `about:debugging#/runtime/this-firefox`
2. Click "Load Temporary Add-on"
3. Select `clients/browser-extension/manifest.json`

### 3. Configure the Extension

1. Click the extension icon, then click the gear icon (or right-click the extension icon and select "Options")
2. Enter your Ratatoskr server URL (e.g., `https://ratatoskr.example.com`)
3. Enter your API key (the JWT `access_token` from step 1)
4. Click "Test Connection" -- you should see a success message
5. Optionally set default tags and auto-summarize preference
6. Click "Save"

## Usage

### Save Current Page

- **Click** the extension icon to open the popup, then click "Save to Ratatoskr"
- **Keyboard shortcut**: `Ctrl+Shift+S` (Mac: `Cmd+Shift+S`) for instant save with defaults
- **Right-click** on a page and select "Save to Ratatoskr"

### Save with Selected Text

1. Select text on a page
2. Right-click and choose "Save selection to Ratatoskr"
3. The selected text is saved as a note alongside the URL

### Tag Assignment

- The popup shows your 8 most-used tags as toggleable chips
- Click tags to toggle them on/off before saving
- Type a new tag name in the text input to create it on the fly

### Recent Saves

The bottom of the popup shows your last 5 saved pages with relative timestamps.

## Security Notes

- API credentials are stored in Chrome/Firefox sync storage (encrypted by the browser)
- All API calls use HTTPS with Bearer token authentication
- The extension only accesses the active tab's URL and title -- no browsing data is collected
- Secret-key auth tokens can be rotated via `POST /v1/auth/secret-keys/{id}/rotate`
- Revoke access anytime via `POST /v1/auth/secret-keys/{id}/revoke`

## Icons

Source SVG: `icons/icon.svg`.

To regenerate PNGs from the SVG:

```bash
python icons/generate-icons.py
```

Requires `cairosvg` (`pip install cairosvg`) or use any SVG-to-PNG converter at 16x16, 48x48, and 128x128.

## Design Tokens

The browser extension does not currently consume the external web application's design-token build. Its CSS is maintained locally until the extension gains an explicit token import/build step.

## Project Structure

```
clients/browser-extension/
  background/       Service worker for API calls, shortcuts, context menus
  content/          Content script for text selection
  icons/            Extension icons (SVG source + PNG outputs)
  options/          Settings page (server URL, API key, defaults)
  popup/            Popup UI (save form, tag picker, recent saves)
  manifest.json     Extension manifest (Manifest V3)
```

## Troubleshooting

### "Not configured" message in popup

Open extension options and enter your server URL and API key.

### "Test Connection" fails

- Verify the server URL includes the protocol (`https://`)
- Verify the API key is a valid JWT token (not the raw secret)
- Check that the server is reachable from your network
- If using secret-key auth, ensure `SECRET_LOGIN_ENABLED=true` is set on the server

### Badge shows error after saving

- Check the browser console (`Ctrl+Shift+J`) for error details
- Common causes: expired JWT token, network error, server down
- If the token expired, obtain a new one via secret-key login and update the extension settings
