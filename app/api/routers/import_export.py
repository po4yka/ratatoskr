"""Import/Export endpoints for bookmark data."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import ValidationError as PydanticValidationError
from starlette.responses import StreamingResponse

from app.api.exceptions import APIException, ErrorCode
from app.api.models.requests import ImportOptionsRequest
from app.api.models.responses import (
    ImportJobDeletionResponse,
    ImportJobListResponse,
    ImportJobResponse,
    TypedSuccessResponse,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.api.services.import_export_service import ImportExportService
from app.config.settings import load_config
from app.core.logging_utils import get_logger
from app.domain.services.import_export import (
    CsvExporter,
    FormatDetector,
    JsonExporter,
    NetscapeHtmlExporter,
)
from app.domain.services.import_parsers import PARSER_REGISTRY
from app.tasks.import_tasks import process_import_job

logger = get_logger(__name__)
router = APIRouter()

_EXPORT_FORMAT_MAP: dict[str, tuple[type, str, str]] = {
    "json": (JsonExporter, "application/json", "bookmarks.json"),
    "csv": (CsvExporter, "text/csv", "bookmarks.csv"),
    "html": (NetscapeHtmlExporter, "text/html", "bookmarks.html"),
}

_UPLOAD_CHUNK_SIZE = 64 * 1024  # 64 KB


async def _read_bounded(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in 64 KB chunks; raise 413 if max_bytes is exceeded."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise APIException(
                message=f"File exceeds maximum allowed size of {max_bytes} bytes",
                error_code=ErrorCode.VALIDATION_ERROR,
                status_code=413,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _bookmark_to_dict(bookmark: Any) -> dict[str, Any]:
    """Serialize a parsed bookmark for Taskiq JSON transport."""
    return {
        "url": bookmark.url,
        "title": bookmark.title,
        "tags": bookmark.tags,
        "notes": bookmark.notes,
        "created_at": bookmark.created_at.isoformat() if bookmark.created_at else None,
        "collection_name": bookmark.collection_name,
        "highlights": bookmark.highlights,
        "extra": bookmark.extra,
    }


# ---------------------------------------------------------------------------
# Import endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/import",
    status_code=201,
    response_model=TypedSuccessResponse[ImportJobResponse],
)
async def import_bookmarks(
    file: UploadFile = File(...),
    options: str = Form(default="{}"),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Import bookmarks from an uploaded file."""
    cfg = load_config(allow_stub_telegram=True).import_export

    # Parse options
    try:
        opts = ImportOptionsRequest.model_validate_json(options).model_dump()
    except (PydanticValidationError, TypeError, ValueError) as err:
        raise APIException(
            message="Invalid import options",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        ) from err

    # Read file content with size limit
    content = await _read_bounded(file, cfg.max_upload_bytes)
    if not content:
        raise APIException(
            message="Uploaded file is empty",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    # Detect format
    filename = file.filename or "unknown"
    source_format = FormatDetector.detect(filename, content)
    if source_format == "unknown" or source_format not in PARSER_REGISTRY:
        raise APIException(
            message=f"Unrecognized import format for file: {filename}",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    # Parse bookmarks
    parser_cls = PARSER_REGISTRY[source_format]
    parser = parser_cls()
    bookmarks = parser.parse(content)

    if not bookmarks:
        raise APIException(
            message="No bookmarks found in uploaded file",
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    if len(bookmarks) > cfg.max_items:
        raise APIException(
            message=(f"Import contains {len(bookmarks)} items; maximum allowed is {cfg.max_items}"),
            error_code=ErrorCode.VALIDATION_ERROR,
            status_code=400,
        )

    service = ImportExportService()
    job = await service.create_import_job(
        user_id=user["user_id"],
        source_format=source_format,
        file_name=filename,
        total_items=len(bookmarks),
        options=opts,
    )

    await process_import_job.kiq(
        job_id=job["id"],
        user_id=user["user_id"],
        bookmarks_json=[_bookmark_to_dict(b) for b in bookmarks],
        options=opts,
    )

    return success_response(job)


@router.get(
    "/import/{job_id}",
    response_model=TypedSuccessResponse[ImportJobResponse],
)
async def get_import_job(
    job_id: int,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Get import job status and progress."""
    job = await ImportExportService().get_import_job(job_id=job_id, user_id=user["user_id"])
    return success_response(job)


@router.get("/import", response_model=TypedSuccessResponse[ImportJobListResponse])
async def list_import_jobs(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List user's import jobs."""
    jobs = await ImportExportService().list_import_jobs(user_id=user["user_id"])
    return success_response({"jobs": jobs})


@router.delete(
    "/import/{job_id}",
    response_model=TypedSuccessResponse[ImportJobDeletionResponse],
)
async def delete_import_job(
    job_id: int,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete an import job."""
    await ImportExportService().delete_import_job(job_id=job_id, user_id=user["user_id"])
    return success_response({"deleted": True, "id": job_id})


# ---------------------------------------------------------------------------
# Export endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/export",
    response_class=StreamingResponse,
    response_model=None,
)
async def export_bookmarks(
    format: str = Query(default="json", pattern="^(json|csv|html)$"),
    tag: str | None = Query(default=None),
    collection_id: int | None = Query(default=None),
    user: dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    """Export user summaries in the requested format."""
    summaries = await ImportExportService().export_summaries(
        user_id=user["user_id"],
        tag=tag,
        collection_id=collection_id,
    )

    exporter_cls, content_type, filename = _EXPORT_FORMAT_MAP[format]
    exporter = exporter_cls()
    body = exporter.serialize(summaries)

    return StreamingResponse(
        iter([body]),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
