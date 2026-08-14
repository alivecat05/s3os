"""Small os-style helpers for S3 buckets."""

from .core import (
    S3OS,
    check_bucket_permissions,
    download_file,
    list_all_buckets,
    list_objects_in_bucket,
)

__all__ = [
    "S3OS",
    "check_bucket_permissions",
    "download_file",
    "list_all_buckets",
    "list_objects_in_bucket",
]

__version__ = "0.2.0"
