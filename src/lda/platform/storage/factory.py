from __future__ import annotations

import boto3
from botocore.config import Config

from lda.platform.configuration.settings import ObjectStorageSettings
from lda.platform.storage.s3 import S3ObjectStore


def create_object_store(settings: ObjectStorageSettings) -> S3ObjectStore:
    client = boto3.client(
        "s3",
        region_name=settings.region,
        # SigV4: presigned GETs of SSE-KMS objects fail under botocore's legacy SigV2 default.
        config=Config(signature_version="s3v4"),
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key_id.get_secret_value()
        if settings.access_key_id
        else None,
        aws_secret_access_key=(
            settings.secret_access_key.get_secret_value() if settings.secret_access_key else None
        ),
    )
    return S3ObjectStore(client, settings)
