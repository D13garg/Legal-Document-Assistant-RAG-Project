"""Object-store readiness probe (ADR-045): is the bucket reachable with these credentials?

A bucket that does not exist, a refused connection and a credentials problem are three different
operator mistakes and each gets its own stable code. Messages never contain the endpoint,
credentials, keys or object content. This does not change the adapter or its semantics.
"""

from __future__ import annotations

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from lda.platform.configuration.settings import ObjectStorageSettings
from lda.shared.kernel.errors import ExternalProviderError, NotFoundError


def check_bucket_ready(settings: ObjectStorageSettings, *, timeout_seconds: float = 5.0) -> None:
    client = boto3.client(
        "s3",
        region_name=settings.region,
        config=Config(
            signature_version="s3v4",
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            retries={"max_attempts": 1},
        ),
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key_id.get_secret_value()
        if settings.access_key_id
        else None,
        aws_secret_access_key=(
            settings.secret_access_key.get_secret_value() if settings.secret_access_key else None
        ),
    )
    try:
        client.head_bucket(Bucket=settings.bucket)
    except ClientError as exc:
        status = str(exc.response.get("Error", {}).get("Code", ""))
        if status in {"404", "NoSuchBucket", "NotFound"}:
            raise NotFoundError("object_store_bucket_missing") from None
        if status in {"401", "403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
            raise ExternalProviderError("object_store_access_denied") from None
        raise ExternalProviderError("object_store_unavailable") from None
    except BotoCoreError:
        raise ExternalProviderError("object_store_unavailable") from None
