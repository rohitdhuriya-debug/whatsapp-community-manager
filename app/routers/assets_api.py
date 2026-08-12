"""The asset library: every PDF / spreadsheet this app has generated."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from ..services import assets

router = APIRouter(prefix="/api/assets", tags=["assets"])

MIME_BY_KIND = {"pdf": assets.PDF_MIME, "excel": assets.XLSX_MIME}


@router.get("")
def list_assets(limit: int = 60) -> list[dict[str, Any]]:
    return assets.list_assets(limit)


def _resolve(filename: str):
    try:
        return assets.resolve_asset(filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{filename}/preview")
def preview(filename: str) -> dict[str, Any]:
    path = _resolve(filename)
    kind = "excel" if path.suffix in (".xlsx", ".xls") else "pdf"
    return assets.preview(str(path), kind)


@router.get("/{filename}/page/{index}")
def page_image(filename: str, index: int) -> Response:
    """One PDF page as a PNG, so the preview needs no browser PDF plugin."""
    path = _resolve(filename)
    if path.suffix != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDFs have page images.")
    try:
        png = assets.render_page(path, index)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not render page: {exc}") from exc
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/{filename}/inline")
def inline(filename: str) -> FileResponse:
    path = _resolve(filename)
    kind = "excel" if path.suffix in (".xlsx", ".xls") else "pdf"
    return FileResponse(
        path,
        media_type=MIME_BY_KIND.get(kind, "application/octet-stream"),
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


@router.get("/{filename}")
def download(filename: str) -> FileResponse:
    path = _resolve(filename)
    kind = "excel" if path.suffix in (".xlsx", ".xls") else "pdf"
    return FileResponse(
        path, media_type=MIME_BY_KIND.get(kind, "application/octet-stream"),
        filename=path.name,
    )
