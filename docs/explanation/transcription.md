# CPU-Only Transcription

How ratatoskr turns voice messages, audio URLs, and captionless videos into text without leaving the host.

**Audience:** Operators choosing whether to enable transcription, and contributors debugging the ASR adapter. **Type:** Explanation. **Related:** [`docs/reference/environment-variables.md#transcription-cpu-only-asr`](../reference/environment-variables.md#transcription-cpu-only-asr). **Source:** [`app/adapters/transcription/`](../../app/adapters/transcription/), [`app/config/transcription.py`](../../app/config/transcription.py), [`app/adapters/telegram/command_handlers/transcribe_handler.py`](../../app/adapters/telegram/command_handlers/transcribe_handler.py), [`app/adapters/telegram/routing/voice_message_processor.py`](../../app/adapters/telegram/routing/voice_message_processor.py).

## Why it exists

Three concrete user-facing needs converged on the same engine:

1. **`/transcribe <url>`** — paste a TikTok / YouTube / SoundCloud / direct-media URL and get a transcript back.
2. **Voice and audio messages** — forward a Telegram voice memo or audio file and have it transcribed automatically.
3. **Captionless YouTube videos** — when `youtube-transcript-api` and VTT fallback both return nothing, transcribe the downloaded audio so the summary pipeline still has something to work with.

All three share the same requirements: CPU-only (the bot runs on a Pi-class host), no cloud APIs (privacy + cost), no PyTorch (image size), and one engine instance across all three so we do not load the ~80 MB ONNX recognizer three times.

## Engine

The adapter wraps sherpa-onnx with the [Kroko English streaming Zipformer](https://huggingface.co/Banafo/Kroko-ASR) by default. Other languages (Dutch, French, German, Italian, Portuguese, Spanish, Swedish, Swiss German, Hebrew, Turkish) are a one-line `--model` swap — drop the matching Kroko bundle into a directory and point `TRANSCRIPTION_MODEL_PATH` at it.

Diarization is opt-in via `TRANSCRIPTION_DIARIZATION_ENABLED=true`. It adds a second pass through:

- a **segmentation** model (`pyannote-3.0` CC-BY-4.0 default, `reverb-v1` non-commercial opt-in via `TRANSCRIPTION_DIARIZATION_MODEL=reverb`)
- a **speaker-embedding** model (3D-Speaker CAM++, Apache-2.0)
- **FastClustering** to group voiceprints into speaker IDs

All three are ONNX; the entire diarization stack adds zero new runtime dependencies beyond what sherpa-onnx already brings.

## Pipeline

```
                      input (URL or local file or Telegram media)
                                          |
                                          v
                              +-----------+-----------+
                              | media_fetcher (URL)   |  (yt-dlp, in-process)
                              |   OR Telethon download|
                              +-----------+-----------+
                                          |
                                          v
                              ffmpeg --> 16kHz mono float32 PCM
                                          |          \
                                          |           --> 1.0x decode (diarization only;
                                          |                speed-up degrades segmentation)
                                          |
                                          v (speed=1.5x by default)
                              sherpa-onnx OnlineRecognizer
                              (Kroko streaming Zipformer, CPU, greedy decode)
                                          |
                                          v
                              tokens + per-token timestamps
                                          |
                                          v
                              group on .!? -> sentences (start_sec scaled by speed)
                                          |
                                          | (if --diarize)
                                          v
                              attach SPEAKER_xx per sentence start time
                                          |
                                          v
                              TranscriptionResult
```

The same `TranscriptionService` instance is reused across the three triggers via a process-wide singleton keyed on the config (see `get_or_create_transcription_service` in [`app/adapters/transcription/__init__.py`](../../app/adapters/transcription/__init__.py)). The sherpa-onnx recognizer is lazy-loaded under an `asyncio.Lock` on first call and held for the process lifetime. Per-call inference runs in `asyncio.to_thread` so the event loop is never blocked.

## Trigger surfaces

### 1. `/transcribe` command

[`app/adapters/telegram/command_handlers/transcribe_handler.py`](../../app/adapters/telegram/command_handlers/transcribe_handler.py)

Two invocation forms:

```
/transcribe https://www.tiktok.com/@user/video/...
/transcribe                       <-- as a reply to a voice/audio/video_note/video message
```

The URL form fetches via in-process `yt_dlp.YoutubeDL` into a temp directory; the reply form downloads via Telethon's `Message.download_media`. The handler is only registered when `TRANSCRIPTION_ENABLED=true`.

### 2. Voice / audio / video_note auto-handler

[`app/adapters/telegram/routing/voice_message_processor.py`](../../app/adapters/telegram/routing/voice_message_processor.py)

A new branch in `MessageContentRouter` runs the processor when no other handler claims the message. Gated on `TRANSCRIPTION_ENABLED=true and TRANSCRIPTION_AUTO_VOICE=true`. Currently the transcript is sent back to the user as a Telegram reply only — it is not persisted as a `Summary` row in v1; durable archiving for voice messages is a planned follow-up.

### 3. YouTube pipeline auto-fill

[`app/adapters/youtube/download_pipeline.py`](../../app/adapters/youtube/download_pipeline.py)

When `youtube-transcript-api` returns nothing and VTT subtitle parsing also returns nothing, and `TRANSCRIPTION_AUTO_URL_PIPELINE=true`, the pipeline transcribes the downloaded video file and stores the result in `VideoSourceRequest.audio_transcript_text`. The existing summary pipeline picks this up automatically through `MetadataDrivenVideoSourceExtractor` and the `audio_transcript_text` plumbing in [`app/adapters/meta/platform_extractor.py`](../../app/adapters/meta/platform_extractor.py).

Failures in this path are logged and turned into `None` rather than raised, so the caller's existing "no transcript or subtitles available" error still fires when every path is exhausted. This keeps the YouTube error surface unchanged — transcription is purely additive.

## Operational notes

- **Model location.** Mount `/data/models/` as a persistent volume in production so the ~80 MB ASR bundle does not re-download on every container restart.
- **Long media.** `TRANSCRIPTION_MAX_DURATION_SEC` (default 1800s) refuses anything longer up-front, before any ffmpeg work. Increase only after confirming your host can stay responsive for the duration.
- **Speed vs. accuracy.** Default `TRANSCRIPTION_SPEED=1.5` shaves ~30% off CPU time with minimal accuracy cost. Try 2.0 for clean speech, 1.0 for noisy or fast-speech sources. Output timestamps stay in original-audio time regardless.
- **Long transcripts.** Anything over ~4000 characters is uploaded as a `.txt` attachment via `Message.reply_document` rather than truncated.
- **Diarization licenses.** `pyannote-3.0` is CC-BY-4.0 (attribution only — safe default). `reverb-v1` is **non-commercial** — the adapter logs a license notice on first download and you should verify the Rev.ai model card permits your use case before flipping `TRANSCRIPTION_DIARIZATION_MODEL=reverb`.
- **Limits inherited from the upstream stack.** No overlapping-speech handling (each moment is assigned to exactly one speaker). Speaker labels are per-run only — `SPEAKER_00` is not the same person across different files. Auto speaker-count detection weakens above ~7 speakers; pass an explicit count when you know it.

## Testing

Service, handler, and voice-processor tests mock at the engine seam so they run with no sherpa-onnx and no ffmpeg installed:

- [`tests/test_transcription_service.py`](../../tests/test_transcription_service.py)
- [`tests/test_transcribe_handler.py`](../../tests/test_transcribe_handler.py)
- [`tests/test_voice_message_processor.py`](../../tests/test_voice_message_processor.py)

Real-binary integration coverage (sherpa-onnx wheel + a fixture WAV) is intentionally not in CI — it belongs in a fixture-gated test that does not pay the ~80 MB model-download cost on every run.
