"""Direct PDF download and text-extraction provider (PyMuPDF / fitz)."""

from __future__ import annotations

import asyncio
import io
import time
from urllib.parse import urljoin

from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe_async, make_safe_async_client

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 60
_DEFAULT_MAX_PDF_MB = 20
_MIN_EXTRACTED_CHARS = 100
_PDF_MAGIC = b"%PDF-"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
}


def _is_pdf_url(url: str) -> bool:
    """True only when the URL path ends with .pdf (strips query/fragment first)."""
    path = url.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0].lower()
    return path.endswith(".pdf")


def _extract_text_sync(pdf_bytes: bytes) -> str:
    """CPU-bound: extract text from PDF bytes via PyMuPDF."""
    try:
        import fitz
    except ImportError as exc:
        msg = "PyMuPDF (fitz) is not installed"
        raise RuntimeError(msg) from exc

    doc = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    try:
        pages: list[str] = [page.get_text() for page in doc]
        return "\n\n".join(pages).strip()
    finally:
        doc.close()


class DirectPDFProvider:
    """Scraper chain provider that downloads and extracts PDF URLs using PyMuPDF.

    Fast-fails for any URL whose path does not end with .pdf so it adds
    negligible overhead for normal HTML pages.
    """

    def __init__(
        self,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        *,
        max_pdf_mb: int = _DEFAULT_MAX_PDF_MB,
        min_text_length: int = _MIN_EXTRACTED_CHARS,
    ) -> None:
        self._timeout_sec = timeout_sec
        self._max_pdf_bytes = max_pdf_mb * 1024 * 1024
        self._min_text_length = min_text_length

    @property
    def provider_name(self) -> str:
        return "direct_pdf"

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        if not _is_pdf_url(url):
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="direct_pdf: not a .pdf URL",
                source_url=url,
                endpoint="direct_pdf",
            )

        started = time.perf_counter()
        try:
            pdf_bytes = await self._fetch_pdf(url)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.debug(
                "direct_pdf_fetch_failed",
                extra={"url": url, "error": str(exc), "error_type": type(exc).__name__},
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"direct_pdf fetch failed: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="direct_pdf",
            )

        latency_fetch = int((time.perf_counter() - started) * 1000)

        if pdf_bytes is None:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="direct_pdf: response is not a valid PDF",
                latency_ms=latency_fetch,
                source_url=url,
                endpoint="direct_pdf",
            )

        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, _extract_text_sync, pdf_bytes)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.debug(
                "direct_pdf_extraction_failed",
                extra={"url": url, "error": str(exc)},
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"direct_pdf: extraction failed: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="direct_pdf",
            )

        latency = int((time.perf_counter() - started) * 1000)

        if len(text) < self._min_text_length:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=(
                    f"direct_pdf: extracted text too short ({len(text)} < {self._min_text_length} chars)"
                ),
                latency_ms=latency,
                source_url=url,
                endpoint="direct_pdf",
            )

        logger.info(
            "direct_pdf_extracted",
            extra={
                "url": url,
                "chars": len(text),
                "size_kb": len(pdf_bytes) // 1024,
                "latency_ms": latency,
                "request_id": request_id,
            },
        )

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=text,
            latency_ms=latency,
            source_url=url,
            endpoint="direct_pdf",
        )

    async def _fetch_pdf(self, url: str) -> bytes | None:
        """Stream-download with size, magic-byte guards, and SSRF-safe redirect handling.

        Returns None if the response is not a valid PDF.
        """
        overall_timeout = self._timeout_sec + 5
        async with asyncio.timeout(overall_timeout):
            async with make_safe_async_client(
                follow_redirects=False, timeout=self._timeout_sec
            ) as client:
                current_url = url
                for _ in range(5):
                    safe, reason = await is_url_safe_async(current_url)
                    if not safe:
                        raise ValueError(f"SSRF blocked redirect target: {reason}")
                    async with client.stream("GET", current_url, headers=_HEADERS) as resp:
                        if resp.status_code in {301, 302, 303, 307, 308}:
                            location = resp.headers.get("location")
                            await resp.aclose()
                            if not location:
                                return None
                            current_url = urljoin(current_url, location)
                            continue

                        if resp.status_code != 200:
                            return None

                        ctype = resp.headers.get("content-type", "").lower()
                        ctype_is_pdf = "application/pdf" in ctype

                        cl = resp.headers.get("content-length")
                        if cl:
                            try:
                                if int(cl) > self._max_pdf_bytes:
                                    return None
                            except ValueError:
                                pass

                        chunks: list[bytes] = []
                        total = 0
                        magic_ok: bool | None = None  # None = not checked yet

                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            total += len(chunk)
                            if total > self._max_pdf_bytes:
                                return None
                            chunks.append(chunk)

                            if magic_ok is None and total >= 5:
                                head = b"".join(chunks)[:5]
                                magic_ok = head.startswith(_PDF_MAGIC)
                                if not magic_ok and not ctype_is_pdf:
                                    return None

                        return b"".join(chunks)
                # Exhausted redirect hops
                raise ValueError("Too many redirects")

    async def aclose(self) -> None:
        pass
