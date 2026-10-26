from __future__ import annotations

import typing

from botocore.exceptions import BotoCoreError, ClientError

from lda.platform.configuration.settings import ObjectStorageSettings
from lda.shared.kernel.errors import ConflictError, ExternalProviderError, NotFoundError
from lda.shared.ports.object_store import StoredObject, TenantScopedKey

_MISSING = {"NoSuchKey", "404", "NotFound"}
_PRECONDITION = {"PreconditionFailed", "412", "ConditionalRequestConflict"}


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class S3ObjectStore:
    """S3-compatible ObjectStore. Keys are always tenant-prefixed (TenantScopedKey)."""

    def __init__(self, client: typing.Any, settings: ObjectStorageSettings) -> None:
        self._client = client
        self._bucket = settings.bucket
        self._kms_key_id = settings.kms_key_id
        self._presign_max = settings.presign_max_seconds

    def put(
        self, key: TenantScopedKey, body: bytes, *, content_type: str, overwrite: bool = False
    ) -> StoredObject:
        args: dict[str, typing.Any] = {
            "Bucket": self._bucket,
            "Key": key.render(),
            "Body": body,
            "ContentType": content_type,
        }
        if not overwrite:
            args["IfNoneMatch"] = "*"
        if self._kms_key_id:
            args.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=self._kms_key_id)
        try:
            self._client.put_object(**args)
        except ClientError as exc:
            if _code(exc) in _PRECONDITION:
                raise ConflictError("object_exists") from None
            raise ExternalProviderError("object_store_unavailable", detail=_code(exc)) from None
        except BotoCoreError:
            raise ExternalProviderError("object_store_unavailable") from None
        return StoredObject(key, len(body))

    def get(self, key: TenantScopedKey) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key.render())
            data: bytes = response["Body"].read()
            return data
        except ClientError as exc:
            if _code(exc) in _MISSING:
                raise NotFoundError("object_not_found") from None
            raise ExternalProviderError("object_store_unavailable", detail=_code(exc)) from None
        except BotoCoreError:
            raise ExternalProviderError("object_store_unavailable") from None

    def exists(self, key: TenantScopedKey) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key.render())
        except ClientError as exc:
            if _code(exc) in _MISSING:
                return False
            raise ExternalProviderError("object_store_unavailable", detail=_code(exc)) from None
        except BotoCoreError:
            raise ExternalProviderError("object_store_unavailable") from None
        return True

    def delete(self, key: TenantScopedKey) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key.render())
        except (ClientError, BotoCoreError):
            raise ExternalProviderError("object_store_unavailable") from None

    def presign_get(self, key: TenantScopedKey, *, expires_seconds: int) -> str:
        if not 1 <= expires_seconds <= self._presign_max:
            raise ValueError("expires_seconds out of allowed range")
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key.render()},
            ExpiresIn=expires_seconds,
        )
        return url
