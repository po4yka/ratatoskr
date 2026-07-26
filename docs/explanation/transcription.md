# CPU-Only Transcription

How ratatoskr turns voice messages, audio URLs, and captionless videos into text without leaving the host.

**Audience:** Operators choosing whether to enable transcription, and contributors debugging the ASR adapter. **Type:** Explanation. **Related:** [`docs/reference/environment-variables.md#media-and-transcription`](../reference/environment-variables.md#media-and-transcription). **Source:** [`app/adapters/transcription/`](../../app/adapters/transcription/), [`app/config/transcription.py`](../../app/config/transcription.py), [`app/adapters/telegram/command_handlers/transcribe_handler.py`](../../app/adapters/telegram/command_handlers/transcribe_handler.py), [`app/adapters/telegram/routing/voice_message_processor.py`](../../app/adapters/telegram/routing/voice_message_processor.py).

## Why it exists

Three concrete user-facing needs converged on the same engine:

1. **`/transcribe <url>`** — paste a TikTok / YouTube / SoundCloud / direct-media URL and get a transcript back.
2. **Voice and audio messages** — forward a Telegram voice memo or audio file and have it transcribed automatically.
3. **Captionless YouTube videos** — when `youtube-transcript-api` and VTT fallback both return nothing, transcribe the downloaded audio so the summary pipeline still has something to work with.

All three share the same requirements: CPU-only (the bot runs on a Pi-class host), no cloud APIs (privacy + cost), no PyTorch (image size), and one engine instance across all three so we do not load the ~80 MB ONNX recognizer three times.

## Engine

The adapter wraps sherpa-onnx with one of two language presets selected by `TRANSCRIPTION_LANGUAGE`:

- **`en` (default)** — [Kroko English streaming Zipformer](https://huggingface.co/Banafo/Kroko-ASR) via `OnlineRecognizer.from_transducer`. Streaming, BPE tokens with U+2581 word-start marker, Apache-2.0, ~80 MB INT8. Other Kroko languages (Dutch, French, German, Italian, Portuguese, Spanish, Swedish, Swiss German, Hebrew, Turkish) drop in by pointing `TRANSCRIPTION_MODEL_PATH` at a pre-populated directory.
- **`ru`** — [GigaAM-v3 e2e RNN-T](https://huggingface.co/ai-sage/GigaAM-v3) via `OfflineRecognizer.from_transducer`, using the sherpa-onnx-format export at [`Smirnov75/GigaAM-v3-sherpa-onnx`](https://huggingface.co/Smirnov75/GigaAM-v3-sherpa-onnx). MIT-licensed, ~230 MB INT8, ~8.4% WER on Russian benchmarks, char-level Cyrillic tokens, **punctuation and text normalization built into the model output** (no separate post-processing). Offline only — there is no Russian streaming transducer in the sherpa-onnx ecosystem.

The model resolver carries a per-language bundle registry. Upstream filenames are normalized to the canonical `encoder.onnx` / `decoder.onnx` / `joiner.onnx` / `tokens.txt` layout on download (e.g. GigaAM's `gigaam_v3_e2e_rnnt_encoder.onnx` becomes `encoder.onnx` on disk), so the recognizer loader stays language-agnostic.

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

## Streaming vs. offline

The two language presets sit on opposite sides of an architectural split:

| | English (Kroko) | Russian (GigaAM-v3) |
|---|---|---|
| Backend | `OnlineRecognizer` (streaming) | `OfflineRecognizer.from_transducer` |
| Audio consumed | Chunked + tail-padded | Whole buffer in one call |
| User perceives | Reply after full transcribe (currently — bot batches the output) | Same — reply after full transcribe |
| Tokens | BPE with `▁` word-start marker | Character-level Cyrillic, no marker |
| Punctuation | Inferred from token stream + grouped on `.!?` | Emitted by the model itself; grouped on `.!?` |
| Cold-start model load | ~80 MB | ~230 MB |
| Streaming-captions UX possible? | Yes (engine is streaming; current adapter just doesn't expose partial output) | No (offline only — would need an upstream Russian streaming model) |

For the current Telegram-reply use case the difference is invisible — both paths reply after the transcribe completes. If a future UX surfaces partial captions for English, the Russian path will lag behind that capability until a Russian streaming model appears upstream.

## Operational notes

- **Model location.** Mount `/data/models/` as a persistent volume in production so the ASR bundle does not re-download on every container restart (~80 MB for English, ~230 MB for Russian).
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
