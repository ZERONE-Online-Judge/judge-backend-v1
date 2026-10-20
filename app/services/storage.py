import hashlib
import hmac
import time
from datetime import timedelta
from io import BytesIO
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from minio import Minio

from app.settings import settings


class ObjectStorage:
    def __init__(self) -> None:
        self.backend = settings.object_storage_backend
        self._minio_client: Minio | None = None
        self._minio_client_config: tuple[str, str, str, bool] | None = None

    def storage_key(self, contest_id: str, category: str, filename: str) -> str:
        safe_filename = filename.replace("/", "_").replace("\\", "_")
        key = f"contests/{contest_id}/{category}/{safe_filename}"
        self.validate_key(key)
        return key

    @staticmethod
    def validate_key(storage_key: str) -> None:
        if not storage_key or storage_key.startswith("/") or "\\" in storage_key or "\0" in storage_key or any(part in {"", ".", ".."} for part in storage_key.split("/")):
            raise ValueError("Invalid storage key")

    @staticmethod
    def _signature(method: str, storage_key: str, expires: int) -> str:
        message = f"storage\n{method}\n{storage_key}\n{expires}".encode()
        return hmac.new(settings.auth_token_secret.encode(), message, hashlib.sha256).hexdigest()

    def valid_signature(self, method: str, storage_key: str, expires: int, signature: str) -> bool:
        return expires >= int(time.time()) and hmac.compare_digest(self._signature(method, storage_key, expires), signature)

    def _signed_browser_url(self, method: str, storage_key: str) -> str:
        self.validate_key(storage_key)
        expires = int(time.time()) + settings.object_storage_presign_ttl_seconds
        return f"{self._browser_proxy_url(storage_key)}?expires={expires}&signature={self._signature(method, storage_key, expires)}"

    def presigned_put_url(self, storage_key: str) -> str:
        return self._signed_browser_url("PUT", storage_key)

    def presigned_get_url(self, storage_key: str) -> str:
        return self._signed_browser_url("GET", storage_key)

    def internal_presigned_get_url(self, storage_key: str) -> str:
        self.validate_key(storage_key)
        if self.backend != "minio":
            return self._local_file_url(storage_key)
        raw_url = self._client().presigned_get_object(
            settings.object_storage_bucket,
            storage_key,
            expires=timedelta(seconds=settings.object_storage_presign_ttl_seconds),
        )
        public_base = settings.public_base_url.rstrip("/")
        public_parts = urlsplit(public_base)
        raw_parts = urlsplit(raw_url)
        path = f"/minio{raw_parts.path}"
        return urlunsplit((public_parts.scheme, public_parts.netloc, path, raw_parts.query, ""))

    def read_bytes(self, storage_key: str) -> bytes:
        self.validate_key(storage_key)
        if self.backend == "minio":
            response = self._client().get_object(settings.object_storage_bucket, storage_key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        path = Path(settings.local_object_storage_root) / storage_key
        return path.read_bytes()

    @contextmanager
    def open_reader(self, storage_key: str, *, offset: int = 0, length: int | None = None):
        """Read large archive objects without loading the whole file in memory."""
        self.validate_key(storage_key)
        if self.backend == "minio":
            response = self._client().get_object(
                settings.object_storage_bucket, storage_key, offset=offset, length=length or 0,
            )
            try:
                yield response
            finally:
                response.close()
                response.release_conn()
        else:
            with (Path(settings.local_object_storage_root) / storage_key).open("rb") as source:
                source.seek(offset)
                yield source

    def write_stream(self, storage_key: str, source, size: int, content_type: str) -> None:
        self.validate_key(storage_key)
        if self.backend == "minio":
            self._client().put_object(settings.object_storage_bucket, storage_key, source,
                                     length=size, content_type=content_type)
            return
        path = Path(settings.local_object_storage_root) / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as target:
            import shutil
            shutil.copyfileobj(source, target, length=1024 * 1024)

    def read_text(self, storage_key: str) -> str:
        return self.read_bytes(storage_key).decode("utf-8")

    def size_bytes(self, storage_key: str) -> int | None:
        self.validate_key(storage_key)
        if self.backend == "minio":
            try:
                stat = self._client().stat_object(
                    settings.object_storage_bucket,
                    storage_key,
                )
            except Exception:
                return None
            return int(stat.size)
        path = Path(settings.local_object_storage_root) / storage_key
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return None

    def write_bytes(self, storage_key: str, content: bytes, content_type: str = "application/octet-stream") -> None:
        self.validate_key(storage_key)
        if self.backend == "minio":
            self._client().put_object(
                settings.object_storage_bucket,
                storage_key,
                BytesIO(content),
                length=len(content),
                content_type=content_type,
            )
            return
        path = Path(settings.local_object_storage_root) / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def write_text(self, storage_key: str, content: str, content_type: str = "text/plain") -> None:
        self.write_bytes(storage_key, content.encode("utf-8"), content_type)

    def delete(self, storage_key: str) -> None:
        self.validate_key(storage_key)
        if self.backend == "minio":
            self._client().remove_object(settings.object_storage_bucket, storage_key)
            return
        path = Path(settings.local_object_storage_root) / storage_key
        if path.exists():
            path.unlink()

    def _client(self) -> Minio:
        parsed = urlparse(settings.object_storage_endpoint)
        endpoint = parsed.netloc or parsed.path
        secure = settings.object_storage_secure or parsed.scheme == "https"
        config = (
            endpoint,
            settings.object_storage_access_key,
            settings.object_storage_secret_key,
            secure,
        )
        if self._minio_client is not None and self._minio_client_config == config:
            return self._minio_client
        self._minio_client = Minio(
            endpoint,
            access_key=settings.object_storage_access_key,
            secret_key=settings.object_storage_secret_key,
            secure=secure,
        )
        self._minio_client_config = config
        return self._minio_client

    def _local_file_url(self, storage_key: str) -> str:
        path = Path(settings.local_object_storage_root) / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"file://{quote(str(path))}"

    def _browser_proxy_url(self, storage_key: str) -> str:
        return f"/api/storage/objects/{quote(storage_key, safe='/')}"


object_storage = ObjectStorage()
