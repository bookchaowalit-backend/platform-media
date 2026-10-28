import os

from .adapters import S3ObjectStore


def configured_store():
    names = (
        "MEDIA_S3_ENDPOINT",
        "MEDIA_S3_BUCKET",
        "MEDIA_S3_ACCESS_KEY",
        "MEDIA_S3_SECRET_KEY",
    )
    if not all(os.environ.get(n) for n in names):
        return None
    return S3ObjectStore(
        endpoint=os.environ[names[0]],
        bucket=os.environ[names[1]],
        access_key=os.environ[names[2]],
        secret_key=os.environ[names[3]],
        region=os.environ.get("MEDIA_S3_REGION", "auto"),
    )
