# Social Integrations

## Instagram API Scaffold

This project currently keeps public Instagram URL summarization on the existing unauthenticated Meta scraper fallback. The Instagram API client scaffold is for connected-account OAuth and read-only professional-account media lookups only; it is not wired into production content extraction.

Docs verified on 2026-05-23 against Meta's Instagram Platform documentation:

- Auth flow: Instagram API with Instagram Login uses Business Login for Instagram. The authorization endpoint is `https://www.instagram.com/oauth/authorize`, the short-lived token exchange endpoint is `https://api.instagram.com/oauth/access_token`, the long-lived token exchange endpoint is `https://graph.instagram.com/access_token`, and the long-lived token refresh endpoint is `https://graph.instagram.com/refresh_access_token`.
- Scope model: the read-only scaffold requests `instagram_business_basic`. Meta documents additional scopes such as `instagram_business_content_publish`, `instagram_business_manage_messages`, and `instagram_business_manage_comments`, but this project does not request or implement behavior for those scopes.
- Account restrictions: Instagram API with Instagram Login is for Instagram professional accounts, businesses and creators. Meta's media reference states that media reads return only data for media owned by Instagram professional accounts and cannot be used for media owned by personal Instagram accounts.
- Supported read endpoints in this scaffold: `GET /me` for the connected professional account profile, `GET /<IG_ID>/media` for IDs of that account's media objects, and `GET /<IG_MEDIA_ID>` for fields on a specific Instagram media object.
- Token lifetime: Business Login returns a short-lived Instagram User access token. The scaffold exchanges it for a long-lived token; Meta documents long-lived tokens as valid for about 60 days. A long-lived token can be refreshed for another 60 days when it is at least 24 hours old, unexpired, and the app user granted `instagram_business_basic`.

Explicitly unsupported:

- No username/password automation and no logged-in Instagram page scraping.
- No cookie storage as primary auth.
- No private feed access, private post access, or personal-account media access claims.
- No competitor/public-feed scraping through authenticated Instagram APIs.
- No publishing, comment moderation, messaging, insights, ads, tagging, or webhook behavior in this scaffold.

Reference links:

- [Instagram API with Instagram Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/)
- [Business Login for Instagram](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login/)
- [Get Started with Instagram API with Instagram Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/get-started/)
- [IG Media reference](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media)
- [Refresh Access Token reference](https://developers.facebook.com/docs/instagram-platform/reference/refresh_access_token)
