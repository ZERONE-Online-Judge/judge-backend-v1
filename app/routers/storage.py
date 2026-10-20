import re
from urllib.parse import quote
import mimetypes

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, Response

from app.services.authz import require_contest_staff
from app.services.errors import AppError, not_found, permission_denied
from app.services.responses import ok
from app.services.storage import object_storage

router = APIRouter(tags=["storage"])


def _authorize_object(request: Request, storage_key: str, expires: int, signature: str) -> None:
    try:
        object_storage.validate_key(storage_key)
    except ValueError:
        raise AppError(422, "invalid_storage_key", "Invalid storage key.")
    if signature and object_storage.valid_signature(request.method, storage_key, expires, signature):
        return
    # Editors can inspect testcase/source text with their current token. Images use
    # short-lived signed URLs because browser image requests carry no bearer token.
    parts = storage_key.split("/")
    if request.method == "GET" and len(parts) >= 3 and parts[0] == "contests":
        require_contest_staff(request, parts[1], "contest.problem.resource.view")
        return
    raise permission_denied("A valid storage URL is required.")


@router.get("/storage/download-url")
def storage_download_url(storage_key: str, request: Request):
    _authorize_object(request, storage_key, 0, "")
    if object_storage.size_bytes(storage_key) is None:
        raise not_found()
    return ok(request, {"download_url": object_storage.presigned_get_url(storage_key) + "&download=true"})


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not size or not any(match.groups()):
        raise ValueError("Unsatisfiable range")
    left, right = match.groups()
    if not left:
        if not int(right):
            raise ValueError("Unsatisfiable range")
        return max(0, size - int(right)), size - 1
    start, end = int(left), min(size - 1, int(right)) if right else size - 1
    if start > end or start >= size:
        raise ValueError("Unsatisfiable range")
    return start, end


@router.get("/storage/objects/{storage_key:path}")
def get_storage_object(storage_key: str, request: Request, expires: int = 0, signature: str = "", download: bool = False):
    _authorize_object(request, storage_key, expires, signature)
    size = object_storage.size_bytes(storage_key)
    if size is None:
        raise not_found()
    headers = {"Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff"}
    try:
        selected = _byte_range(request.headers.get("range"), size)
    except ValueError:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    start, end = selected if selected else (0, size - 1)
    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)
    if selected:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if download:
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(storage_key.rsplit("/", 1)[-1], safe="")

    def stream():
        with object_storage.open_reader(storage_key, offset=start, length=length) as source:
            remaining = length
            while remaining:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    media_type = mimetypes.guess_type(storage_key)[0] or "application/octet-stream"
    return StreamingResponse(stream(), status_code=206 if selected else 200, media_type=media_type, headers=headers)


@router.put("/storage/objects/{storage_key:path}")
async def put_storage_object(storage_key: str, request: Request, expires: int = 0, signature: str = ""):
    _authorize_object(request, storage_key, expires, signature)
    content_type = request.headers.get("content-type", "application/octet-stream")
    object_storage.write_bytes(storage_key, await request.body(), content_type)
    return ok(request, {"storage_key": storage_key})
