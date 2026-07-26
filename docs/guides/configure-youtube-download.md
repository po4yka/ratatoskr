# Configure YouTube Download

YouTube URLs use the dedicated platform extractor in `app/adapters/youtube/`.
The pipeline obtains transcript data, downloads media and subtitles with
`yt-dlp`, persists the extraction artifacts, and passes normalized text into the
summarize graph.

## Runtime requirements

- Enable the `youtube` dependency extra for a local Python installation.
- Install `ffmpeg` for separate audio/video stream merging and transcription
  fallback.
- Give `YOUTUBE_STORAGE_PATH` enough persistent space.

The main Docker image installs `ffmpeg` unless it is deliberately built with
`WITH_FFMPEG=0`.

## Configuration

The checked-in defaults are defined under `youtube:` in
`config/ratatoskr.yaml`:

```yaml
youtube:
  enabled: true
  storage_path: /data/videos
  max_video_size_mb: 500
  max_storage_gb: 100
  auto_cleanup_enabled: true
  cleanup_after_days: 30
  preferred_quality: 1080p
  subtitle_languages: [en, ru]
```

Environment overrides use these names:

| Variable | Purpose |
| --- | --- |
| `YOUTUBE_DOWNLOAD_ENABLED` | Enable the YouTube platform extractor. |
| `YOUTUBE_STORAGE_PATH` | Directory for downloaded media and sidecars. |
| `YOUTUBE_MAX_VIDEO_SIZE_MB` | Reject downloads beyond the per-video limit. |
| `YOUTUBE_MAX_STORAGE_GB` | Storage budget used by the cleanup guard. |
| `YOUTUBE_AUTO_CLEANUP_ENABLED` | Allow automatic removal of expired downloads. |
| `YOUTUBE_CLEANUP_AFTER_DAYS` | Retention period for completed downloads. |
| `YOUTUBE_PREFERRED_QUALITY` | One of `1080p`, `720p`, `480p`, `360p`, `240p`. |
| `YOUTUBE_SUBTITLE_LANGUAGES` | Comma-separated subtitle preference order. |

For a local process, create the configured directory and make it writable by the
runtime user. The Compose deployment persists the common `/data` volume.

## Transcript fallback

The normal order is the YouTube transcript API followed by subtitle files from
`yt-dlp`. When neither yields text, Ratatoskr can transcribe the downloaded media
through the optional shared transcription service.

Enable that fallback with:

```bash
TRANSCRIPTION_ENABLED=true
TRANSCRIPTION_AUTO_URL_PIPELINE=true
```

The transcription provider is `local` by default. It uses the language-specific
sherpa-onnx model under `TRANSCRIPTION_MODEL_PATH`. A remote OpenAI-compatible
provider is selected with `TRANSCRIPTION_PROVIDER=openai` and
`TRANSCRIPTION_API_KEY`; there are no `ENABLE_WHISPER_TRANSCRIPTION` or
`WHISPER_API_KEY` settings.

See [Environment Variables](../reference/environment-variables.md) for the full
transcription configuration.

## Verify

After restarting the affected bot/worker process, send a public YouTube watch,
short, live, embed, or `youtu.be` URL. Verify observed behavior rather than a
fixed response template:

1. progress reaches transcript extraction and media download;
2. the request completes with a summary or a user-visible error containing an
   `Error ID`;
3. the request, download, crawl/source artifacts, and summary exist in
   PostgreSQL;
4. temporary or retained files appear below the configured storage path.

The exact duration and Telegram formatting depend on media length, subtitle
availability, LLM configuration, and current formatter behavior.

## Troubleshooting

- **Merge failure:** run `ffmpeg -version`; inspect the `yt-dlp` error stored for
  the download.
- **No transcript:** confirm the video is public and not age-, region-, or
  membership-restricted; enable transcription fallback if policy permits.
- **Size limit:** increase `YOUTUBE_MAX_VIDEO_SIZE_MB` deliberately or choose a
  lower `YOUTUBE_PREFERRED_QUALITY`.
- **Storage pressure:** inspect `YOUTUBE_STORAGE_PATH`, cleanup settings, and the
  persistent-volume capacity.
- **Authentication/rate limit:** the downloader reports login/age checks and
  YouTube throttling as extraction failures; Ratatoskr does not document a
  browser-cookie bypass as a supported production contract.

For correlation-ID lookup and detailed failure triage, see
[Troubleshooting: YouTube issues](../reference/troubleshooting.md#youtube-issues).
