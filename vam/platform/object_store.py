"""Object storage adapter: the only module that speaks the S3 API.

``ObjectStore`` is the interface UploadService (and later E2 workers) depend on.
``S3ObjectStore`` targets any S3-compatible service that implements multipart
upload, ListParts, presigned UploadPart and Content-MD5 validation (verified
locally against a pinned MinIO release; see docs/engineering/E1_DELIVERY.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol


class ObjectStoreError(RuntimeError):
    """Transient or unexpected storage failure (retryable)."""


class NoSuchUpload(ObjectStoreError):
    """The multipart upload id no longer exists (completed or aborted)."""


class NoSuchObject(ObjectStoreError):
    pass


class InvalidPart(ObjectStoreError):
    """Storage rejected the part list on completion (missing/too small/etag mismatch)."""


@dataclass(frozen=True)
class StoredPart:
    part_number: int
    size: int
    etag: str  # without surrounding quotes


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    etag: str


class ObjectStore(Protocol):
    def ensure_bucket(self) -> None: ...
    def create_multipart(self, key: str, content_type: str) -> str: ...
    def presign_upload_part(self, key: str, upload_id: str, part_number: int, md5_b64: str,
                            size: int, expires_s: int) -> str: ...
    def list_parts(self, key: str, upload_id: str) -> list[StoredPart]: ...
    def complete_multipart(self, key: str, upload_id: str, parts: list[StoredPart]) -> str: ...
    def abort_multipart(self, key: str, upload_id: str) -> None: ...
    def list_multipart_uploads(self, prefix: str) -> list[tuple[str, str, float]]: ...
    def head(self, key: str) -> ObjectInfo: ...
    def iter_object(self, key: str, chunk_size: int = 8 << 20) -> Iterator[bytes]: ...
    def delete(self, key: str) -> None: ...
    def health(self) -> dict: ...


def _strip(etag: str) -> str:
    return (etag or "").strip('"')


class S3ObjectStore:
    def __init__(self, *, endpoint: str, public_endpoint: str, access_key: str, secret_key: str,
                 bucket: str, region: str = "us-east-1", connect_timeout: float = 5, read_timeout: float = 60):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - environment guard
            raise ObjectStoreError('S3 support requires: pip install -e ".[platform]"') from exc
        self.bucket = bucket
        config = Config(signature_version="s3v4", s3={"addressing_style": "path"},
                        connect_timeout=connect_timeout, read_timeout=read_timeout,
                        retries={"max_attempts": 3, "mode": "standard"})
        kwargs = dict(aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name=region, config=config)
        self.client = boto3.client("s3", endpoint_url=endpoint, **kwargs)
        # Presigned URLs embed the host the browser will contact; signing is offline.
        self.signer = boto3.client("s3", endpoint_url=public_endpoint or endpoint, **kwargs)

    def _call(self, fn, *args, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return fn(*args, **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "NoSuchUpload":
                raise NoSuchUpload(code) from exc
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise NoSuchObject(code) from exc
            if code in {"InvalidPart", "InvalidPartOrder", "EntityTooSmall", "BadDigest"}:
                raise InvalidPart(code) from exc
            raise ObjectStoreError(code or "s3_client_error") from exc
        except BotoCoreError as exc:
            raise ObjectStoreError(type(exc).__name__) from exc

    def ensure_bucket(self):
        from botocore.exceptions import ClientError
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._call(self.client.create_bucket, Bucket=self.bucket)

    def create_multipart(self, key, content_type):
        return self._call(self.client.create_multipart_upload, Bucket=self.bucket, Key=key,
                          ContentType=content_type)["UploadId"]

    def presign_upload_part(self, key, upload_id, part_number, md5_b64, size, expires_s):
        # Content-MD5 is part of the signature: the storage rejects a body whose
        # digest differs (BadDigest), so a corrupted part never becomes "uploaded".
        return self.signer.generate_presigned_url(
            "upload_part", ExpiresIn=int(expires_s), HttpMethod="PUT",
            Params={"Bucket": self.bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number,
                    "ContentMD5": md5_b64, "ContentLength": size})

    def list_parts(self, key, upload_id):
        parts, marker = [], 0
        while True:
            page = self._call(self.client.list_parts, Bucket=self.bucket, Key=key, UploadId=upload_id,
                              MaxParts=1000, PartNumberMarker=marker)
            parts += [StoredPart(p["PartNumber"], p["Size"], _strip(p["ETag"])) for p in page.get("Parts", [])]
            if not page.get("IsTruncated"):
                return parts
            marker = page["NextPartNumberMarker"]

    def complete_multipart(self, key, upload_id, parts):
        result = self._call(self.client.complete_multipart_upload, Bucket=self.bucket, Key=key, UploadId=upload_id,
                            MultipartUpload={"Parts": [{"PartNumber": p.part_number, "ETag": f'"{p.etag}"'}
                                                       for p in sorted(parts, key=lambda p: p.part_number)]})
        return _strip(result.get("ETag", ""))

    def abort_multipart(self, key, upload_id):
        try:
            self._call(self.client.abort_multipart_upload, Bucket=self.bucket, Key=key, UploadId=upload_id)
        except NoSuchUpload:
            pass

    def list_multipart_uploads(self, prefix):
        found, key_marker, id_marker = [], None, None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if key_marker:
                kwargs |= {"KeyMarker": key_marker, "UploadIdMarker": id_marker}
            page = self._call(self.client.list_multipart_uploads, **kwargs)
            found += [(u["Key"], u["UploadId"], u["Initiated"].timestamp()) for u in page.get("Uploads", [])]
            if not page.get("IsTruncated"):
                return found
            key_marker, id_marker = page.get("NextKeyMarker"), page.get("NextUploadIdMarker")

    def head(self, key):
        result = self._call(self.client.head_object, Bucket=self.bucket, Key=key)
        return ObjectInfo(int(result["ContentLength"]), _strip(result.get("ETag", "")))

    def iter_object(self, key, chunk_size=8 << 20):
        from botocore.exceptions import BotoCoreError
        body = self._call(self.client.get_object, Bucket=self.bucket, Key=key)["Body"]
        try:
            while True:
                try:
                    chunk = body.read(chunk_size)
                except (BotoCoreError, OSError) as exc:
                    raise ObjectStoreError(type(exc).__name__) from exc
                if not chunk:
                    return
                yield chunk
        finally:
            body.close()

    def delete(self, key):
        self._call(self.client.delete_object, Bucket=self.bucket, Key=key)

    def health(self):
        self._call(self.client.head_bucket, Bucket=self.bucket)
        return {"bucket": self.bucket, "status": "ok"}
