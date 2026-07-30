"""Attachment classification, extraction, and prompt assembly helpers."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import TYPE_CHECKING, Any, Final

from app.adapters.attachment._attachment_shared import _MAX_PDF_TEXT_CHARS, load_prompt
from app.adapters.attachment.image_extractor import ImageExtractor
from app.adapters.attachment.markitdown_extractor import MarkitdownExtractor
from app.adapters.attachment.pdf_extractor import PDFExtractor
from app.adapters.attachment.vision_messages import (
    build_multi_image_vision_messages,
    build_text_with_images_messages,
    build_vision_messages,
)
from app.adapters.telegram_source_helpers import telegram_media_file_name, telegram_media_size
from app.core.lang import LANG_AUTO, LANG_RU, choose_language, detect_language

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.attachment._attachment_llm import AttachmentLLMWorkflowService
    from app.adapters.attachment._attachment_persistence import AttachmentPersistenceService
    from app.adapters.attachment._attachment_shared import AttachmentProcessorContext

_MIME_TO_FORMAT: Final[dict[str, str]] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/epub+zip": "epub",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "text/csv": "csv",
    "text/html": "html",
    "application/json": "json",
    "application/xml": "xml",
    "text/xml": "xml",
}
_DOCUMENT_MIME_TYPES: Final[frozenset[str]] = frozenset(_MIME_TO_FORMAT)


class AttachmentContentService:
    """Processes downloaded attachments into LLM-ready requests."""

    def __init__(
        self,
        context: AttachmentProcessorContext,
        *,
        persistence: AttachmentPersistenceService,
        workflow: AttachmentLLMWorkflowService,
    ) -> None:
        self._context = context
        self._persistence = persistence
        self._workflow = workflow

    def classify_attachment(self, message: Any) -> tuple[str | None, str | None, str | None]:
        """Classify the attachment type."""
        if getattr(message, "photo", None):
            return "image", "image/jpeg", None

        doc = getattr(message, "document", None)
        if not doc:
            return None, None, None

        mime = getattr(doc, "mime_type", "") or ""
        # Telethon's raw Document has no flat file_name, so this was always None
        # and attachment_processing.file_name was never populated.
        fname = telegram_media_file_name(message)
        if mime.startswith("image/"):
            return "image", mime, fname
        if mime == "application/pdf":
            return "pdf", mime, fname
        if mime in _DOCUMENT_MIME_TYPES:
            if not self._context.cfg.attachment.document_processing_enabled:
                return None, None, None
            return "document", mime, fname
        return None, None, None

    def check_size_limits(self, message: Any, file_type: str) -> str | None:
        """Check the attachment size against configuration limits."""
        attachment_cfg = self._context.cfg.attachment
        file_size = telegram_media_size(message)

        if file_size is None:
            # Reading `file_size` alone used to land here for every real
            # Telethon message -- Document exposes `size`, Photo only `sizes` --
            # so ATTACHMENT_MAX_*_SIZE_MB was never enforced and a multi-hundred
            # megabyte upload was downloaded and parsed in-process on the Pi.
            # Kept permissive rather than fail-closed: with the probe fixed this
            # is unreachable for real messages, and refusing on an unknown shape
            # would reject valid uploads. The log makes it diagnosable.
            self._context.logger.warning(
                "attachment_size_unknown",
                extra={"file_type": file_type},
            )
            return None

        if file_type == "image":
            max_bytes = attachment_cfg.max_image_size_mb * 1024 * 1024
            max_mb = attachment_cfg.max_image_size_mb
            label = "Image"
        elif file_type == "document":
            max_bytes = attachment_cfg.max_document_size_mb * 1024 * 1024
            max_mb = attachment_cfg.max_document_size_mb
            label = "Document"
        else:
            max_bytes = attachment_cfg.max_pdf_size_mb * 1024 * 1024
            max_mb = attachment_cfg.max_pdf_size_mb
            label = "PDF"

        if file_size <= max_bytes:
            return None
        return f"{label} too large (max {max_mb}MB)."

    async def download_attachment(self, message: Any) -> str | None:
        """Download the attachment to a temp file."""
        storage_path = self._context.cfg.attachment.storage_path
        os.makedirs(storage_path, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=storage_path)
            os.close(fd)
            path = await message.download(file_name=tmp_path)
            return str(path) if path else None
        except Exception as exc:
            self._context.logger.exception(
                "attachment_download_failed",
                extra={"error": str(exc)},
            )
            return None

    def choose_attachment_language(self, caption: str | None, message: Any) -> str:
        """Determine response language for attachment analysis."""
        text_for_lang = caption or ""
        user_lang_code = getattr(getattr(message, "from_user", None), "language_code", None)
        user_lang = user_lang_code[:2] if user_lang_code else "en"
        detected = detect_language(text_for_lang) if text_for_lang else user_lang
        return choose_language(self._context.cfg.runtime.preferred_lang, detected)

    async def process_downloaded_attachment(
        self,
        *,
        message: Any,
        file_path: str,
        file_type: str,
        mime_type: str | None,
        file_name: str | None,
        caption: str | None,
        correlation_id: str | None,
        interaction_id: int | None,
        status_updater: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        """Create records and dispatch attachment analysis."""
        req_id = await self._persistence.create_request(message, correlation_id, file_type)
        chosen_lang = self.choose_attachment_language(caption, message)
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else None
        await self._persistence.create_attachment_record(
            req_id=req_id,
            file_type=file_type,
            mime_type=mime_type,
            file_name=file_name,
            file_size=file_size,
        )

        if file_type == "image":
            result = await self.process_image(
                file_path=file_path,
                caption=caption,
                chosen_lang=chosen_lang,
                req_id=req_id,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                message=message,
                status_updater=status_updater,
            )
        elif file_type == "document":
            file_format = _MIME_TO_FORMAT.get(mime_type or "", "document")
            result = await self.process_document(
                file_path=file_path,
                file_format=file_format,
                caption=caption,
                chosen_lang=chosen_lang,
                req_id=req_id,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                message=message,
                status_updater=status_updater,
            )
        else:
            result = await self.process_pdf(
                file_path=file_path,
                caption=caption,
                chosen_lang=chosen_lang,
                req_id=req_id,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                message=message,
                status_updater=status_updater,
            )
        return req_id, result

    async def process_downloaded_attachment_bundle(
        self,
        *,
        message: Any,
        file_paths: list[str],
        caption: str | None,
        correlation_id: str | None,
        interaction_id: int | None,
        status_updater: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        """Create records and dispatch multimodal analysis for an image bundle."""

        req_id = await self._persistence.create_request(message, correlation_id, "image")
        chosen_lang = self.choose_attachment_language(caption, message)
        total_size = (
            sum(os.path.getsize(path) for path in file_paths if path and os.path.exists(path))
            or None
        )
        await self._persistence.create_attachment_record(
            req_id=req_id,
            file_type="image_bundle",
            mime_type="image/*",
            file_name=None,
            file_size=total_size,
        )
        result = await self.process_image_bundle(
            file_paths=file_paths,
            caption=caption,
            chosen_lang=chosen_lang,
            req_id=req_id,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            message=message,
            status_updater=status_updater,
        )
        return req_id, result

    async def process_image(
        self,
        *,
        file_path: str,
        caption: str | None,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        interaction_id: int | None,
        message: Any,
        status_updater: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        """Process an image attachment via the vision model."""
        attachment_cfg = self._context.cfg.attachment
        if status_updater:
            await status_updater("🖼 <b>Processing image...</b>")

        try:
            # Image.open + LANCZOS resize + JPEG encode is CPU-bound and can run
            # for seconds on a large photo. The PDF and markitdown branches of
            # this same class already offload; this one did not, so a 100-megapixel
            # upload blocked the loop thread outright -- Telethon stops answering,
            # progress edits do not fire, and every in-flight summarize stalls.
            image_content = await asyncio.to_thread(
                ImageExtractor.extract,
                file_path,
                max_dimension=attachment_cfg.image_max_dimension,
            )
        except ValueError as exc:
            self._context.logger.warning(
                "image_extraction_failed",
                extra={"error": str(exc), "cid": correlation_id},
            )
            await self._context.response_formatter.safe_reply(
                message,
                f"Could not process image: {exc}",
            )
            return None

        system_prompt = load_prompt("image_analysis", chosen_lang)
        lang_label = "Russian" if chosen_lang == LANG_RU else "English"
        user_text = (
            caption
            or f"Analyze this image and provide a structured summary. Respond in {lang_label}."
        )
        if caption:
            user_text = f"{caption}\n\nRespond in {lang_label}."

        messages = build_vision_messages(system_prompt, image_content.data_uri, caption=user_text)
        return await self._workflow.run_summary_workflow(
            messages=messages,
            req_id=req_id,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            chosen_lang=chosen_lang,
            message=message,
            model_override=attachment_cfg.vision_model,
            status_updater=status_updater,
        )

    async def process_image_bundle(
        self,
        *,
        file_paths: list[str],
        caption: str | None,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        interaction_id: int | None,
        message: Any,
        status_updater: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        """Process multiple Telegram images as one multimodal source."""

        attachment_cfg = self._context.cfg.attachment
        if status_updater:
            await status_updater("🖼 <b>Processing image bundle...</b>")

        image_contents = []
        for file_path in file_paths:
            try:
                # Sequential rather than gathered: an album is up to ten images
                # and firing ten threads at once would only crowd the shared
                # default executor on a Pi. Awaiting each one still yields
                # between images, which is what the loop needs.
                image_contents.append(
                    await asyncio.to_thread(
                        ImageExtractor.extract,
                        file_path,
                        max_dimension=attachment_cfg.image_max_dimension,
                    )
                )
            except ValueError as exc:
                self._context.logger.warning(
                    "image_bundle_extraction_failed",
                    extra={"error": str(exc), "cid": correlation_id, "path": file_path},
                )
        if not image_contents:
            await self._context.response_formatter.safe_reply(
                message,
                "Could not process images from this album.",
            )
            return None

        system_prompt = load_prompt("image_analysis", chosen_lang)
        lang_label = "Russian" if chosen_lang == LANG_RU else "English"
        user_text = (
            caption
            or f"Analyze these related Telegram images and provide a structured summary. Respond in {lang_label}."
        )
        if caption:
            user_text = f"{caption}\n\nRespond in {lang_label}."

        image_uris = [image.data_uri for image in image_contents]
        if len(image_uris) == 1:
            messages = build_vision_messages(system_prompt, image_uris[0], caption=user_text)
        else:
            messages = build_multi_image_vision_messages(
                system_prompt,
                image_uris,
                caption=user_text,
            )
        return await self._workflow.run_summary_workflow(
            messages=messages,
            req_id=req_id,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            chosen_lang=chosen_lang,
            message=message,
            model_override=attachment_cfg.vision_model,
            status_updater=status_updater,
        )

    async def process_pdf(
        self,
        *,
        file_path: str,
        caption: str | None,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        interaction_id: int | None,
        message: Any,
        status_updater: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        """Process a PDF attachment."""
        attachment_cfg = self._context.cfg.attachment
        try:

            async def on_pdf_progress(text: str) -> None:
                if status_updater:
                    await status_updater(f"📄 <b>PDF:</b> {text}")

            loop = asyncio.get_running_loop()
            pdf_content = await asyncio.to_thread(
                PDFExtractor.extract,
                file_path,
                max_pages=attachment_cfg.max_pdf_pages,
                max_vision_pages=attachment_cfg.max_vision_pages_per_pdf,
                image_max_dimension=attachment_cfg.image_max_dimension,
                min_image_dimension=attachment_cfg.pdf_min_image_dimension,
                max_embedded_images=attachment_cfg.pdf_max_embedded_images,
                vector_draw_threshold=attachment_cfg.pdf_vector_draw_threshold,
                on_progress=lambda text: asyncio.run_coroutine_threadsafe(
                    on_pdf_progress(text),
                    loop,
                ),
            )
        except ValueError as exc:
            self._context.logger.warning(
                "pdf_extraction_failed",
                extra={"error": str(exc), "cid": correlation_id},
            )
            await self._context.response_formatter.safe_reply(
                message,
                f"Could not process PDF: {exc}",
            )
            return None

        await self._persistence.update_pdf_metadata(req_id, pdf_content)

        self._context.logger.debug(
            "pdf_extracted",
            extra={
                "pages": pdf_content.page_count,
                "is_scanned": pdf_content.is_scanned,
                "image_pages": len(pdf_content.image_pages),
                "embedded_images": len(pdf_content.embedded_images),
                "figure_page_count": pdf_content.figure_page_count,
                "cid": correlation_id,
            },
        )

        if not caption and self._context.cfg.runtime.preferred_lang == LANG_AUTO:
            content_text = pdf_content.text[:2000]
            if content_text.strip():
                detected = detect_language(content_text)
                chosen_lang = choose_language(self._context.cfg.runtime.preferred_lang, detected)

        metadata_header = self.build_pdf_metadata_header(pdf_content)
        system_prompt = load_prompt("pdf_analysis", chosen_lang)
        lang_label = "Russian" if chosen_lang == LANG_RU else "English"
        model_override: str | None = None

        all_image_uris = [img.data_uri for img in pdf_content.image_pages]
        for image in pdf_content.embedded_images:
            if len(all_image_uris) >= attachment_cfg.pdf_max_image_uris_total:
                break
            all_image_uris.append(image.data_uri)

        if all_image_uris:
            model_override = attachment_cfg.vision_model

        if pdf_content.is_scanned and pdf_content.image_pages:
            messages = self._build_scanned_pdf_messages(
                pdf_content=pdf_content,
                metadata_header=metadata_header,
                system_prompt=system_prompt,
                image_uris=all_image_uris,
                caption=caption,
                lang_label=lang_label,
            )
        else:
            messages = self._build_text_pdf_messages(
                pdf_content=pdf_content,
                metadata_header=metadata_header,
                system_prompt=system_prompt,
                image_uris=all_image_uris,
                caption=caption,
                lang_label=lang_label,
            )

        return await self._workflow.run_summary_workflow(
            messages=messages,
            req_id=req_id,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            chosen_lang=chosen_lang,
            message=message,
            model_override=model_override,
            status_updater=status_updater,
        )

    async def process_document(
        self,
        *,
        file_path: str,
        file_format: str,
        caption: str | None,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        interaction_id: int | None,
        message: Any,
        status_updater: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        """Process an office/EPUB/HTML document attachment via markitdown."""
        attachment_cfg = self._context.cfg.attachment

        try:
            if status_updater:
                await status_updater(f"📄 <b>Converting {file_format.upper()} document...</b>")

            doc_content = await asyncio.to_thread(
                MarkitdownExtractor.extract,
                file_path,
                file_format=file_format,
                max_chars=attachment_cfg.max_document_chars,
            )
        except ValueError as exc:
            self._context.logger.warning(
                "document_extraction_failed",
                extra={"error": str(exc), "cid": correlation_id, "format": file_format},
            )
            await self._context.response_formatter.safe_reply(
                message,
                f"Could not process {file_format.upper()} document: {exc}",
            )
            return None

        await self._persistence.update_document_metadata(req_id, doc_content)

        self._context.logger.debug(
            "document_extracted",
            extra={
                "file_format": file_format,
                "chars": len(doc_content.text),
                "truncated": doc_content.truncated,
                "cid": correlation_id,
            },
        )

        if not caption and self._context.cfg.runtime.preferred_lang == LANG_AUTO:
            content_text = doc_content.text[:2000]
            if content_text.strip():
                detected = detect_language(content_text)
                chosen_lang = choose_language(self._context.cfg.runtime.preferred_lang, detected)

        system_prompt = load_prompt("document_analysis", chosen_lang)
        lang_label = "Russian" if chosen_lang == LANG_RU else "English"
        messages = self._build_document_messages(
            doc_content=doc_content,
            system_prompt=system_prompt,
            caption=caption,
            lang_label=lang_label,
        )

        return await self._workflow.run_summary_workflow(
            messages=messages,
            req_id=req_id,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            chosen_lang=chosen_lang,
            message=message,
            model_override=None,
            status_updater=status_updater,
        )

    def _build_document_messages(
        self,
        *,
        doc_content: Any,
        system_prompt: str,
        caption: str | None,
        lang_label: str,
    ) -> list[dict[str, Any]]:
        truncation_note = ""
        if doc_content.truncated:
            truncation_note = f"\n\n[Document truncated to {_MAX_PDF_TEXT_CHARS} characters]"

        user_content = (
            f"Summarize the following {doc_content.file_format.upper()} document to the specified "
            f"JSON schema. Respond in {lang_label}.\n\n{doc_content.text}{truncation_note}"
        )
        if caption:
            user_content = f"User context: {caption}\n\n{user_content}"

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

    def build_pdf_metadata_header(self, pdf_content: Any) -> str:
        """Build a compact metadata header for PDF prompts."""
        metadata_parts: list[str] = []
        metadata = pdf_content.metadata
        if metadata.get("title"):
            metadata_parts.append(f"Title: {metadata['title']}")
        if metadata.get("author"):
            metadata_parts.append(f"Author: {metadata['author']}")
        if metadata.get("subject"):
            metadata_parts.append(f"Subject: {metadata['subject']}")
        if metadata.get("keywords"):
            metadata_parts.append(f"Keywords: {metadata['keywords']}")

        if pdf_content.toc:
            toc_lines: list[str] = []
            for level, title, page in pdf_content.toc[:30]:
                indent = "  " * (level - 1)
                toc_lines.append(f"{indent}- {title} (page {page})")
            if toc_lines:
                metadata_parts.append("Table of Contents:\n" + "\n".join(toc_lines))

        if not metadata_parts:
            return ""
        return "Document Metadata:\n" + "\n".join(metadata_parts) + "\n\n"

    def _build_scanned_pdf_messages(
        self,
        *,
        pdf_content: Any,
        metadata_header: str,
        system_prompt: str,
        image_uris: list[str],
        caption: str | None,
        lang_label: str,
    ) -> list[dict[str, Any]]:
        if pdf_content.text.strip():
            text = f"{metadata_header}{pdf_content.text[:_MAX_PDF_TEXT_CHARS]}"
            user_caption = caption or f"Summarize this PDF document. Respond in {lang_label}."
            if caption:
                user_caption = f"{caption}\n\nRespond in {lang_label}."
            return build_text_with_images_messages(
                system_prompt,
                text,
                image_uris,
                caption=user_caption,
            )

        user_caption = (
            caption
            or f"Analyze these PDF pages and provide a structured summary. Respond in {lang_label}."
        )
        if caption:
            user_caption = f"{caption}\n\nRespond in {lang_label}."
        scanned_caption = f"{metadata_header}{user_caption}" if metadata_header else user_caption
        return build_multi_image_vision_messages(system_prompt, image_uris, caption=scanned_caption)

    def _build_text_pdf_messages(
        self,
        *,
        pdf_content: Any,
        metadata_header: str,
        system_prompt: str,
        image_uris: list[str],
        caption: str | None,
        lang_label: str,
    ) -> list[dict[str, Any]]:
        text = f"{metadata_header}{pdf_content.text[:_MAX_PDF_TEXT_CHARS]}"
        truncation_note = ""
        if pdf_content.truncated:
            truncation_note = (
                "\n\n[Document truncated: showing "
                f"{self._context.cfg.attachment.max_pdf_pages} of {pdf_content.page_count} pages]"
            )

        user_content = (
            "Summarize the following PDF document to the specified JSON schema. "
            f"Respond in {lang_label}.\n\n{text}{truncation_note}"
        )
        if caption:
            user_content = f"User context: {caption}\n\n{user_content}"

        if not image_uris:
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

        # Vision path: keep the JSON schema instruction in the user message.
        # build_text_with_images_messages loses user_content when caption is None,
        # so build the multipart message manually.
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": user_content}]
        for uri in image_uris:
            content_parts.append({"type": "image_url", "image_url": {"url": uri}})
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_parts},
        ]
