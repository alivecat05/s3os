from __future__ import annotations

import base64
import fnmatch
import hashlib
import io
import posixpath
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import cache
from typing import Any, BinaryIO, Literal, TextIO, Union, overload
from urllib.parse import quote

from botocore.exceptions import ClientError

PermissionValue = Union[bool, str, None]
PermissionReport = dict[str, PermissionValue]
OpenMode = Literal["r", "rb", "w", "wb"]
RenameMode = Literal["auto", "native", "copy"]


def _glob_match(key: str, pattern: str) -> bool:
    """Match an S3 key using path-aware glob semantics."""
    key_parts = tuple(key.split("/"))
    pattern_parts = tuple(pattern.split("/"))

    @cache
    def match(key_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return key_index == len(key_parts)

        part = pattern_parts[pattern_index]
        if part == "**":
            return match(key_index, pattern_index + 1) or (
                key_index < len(key_parts) and match(key_index + 1, pattern_index)
            )

        return (
            key_index < len(key_parts)
            and fnmatch.fnmatchcase(key_parts[key_index], part)
            and match(key_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _client_error_message(exc: ClientError) -> str:
    error = exc.response.get("Error", {})
    return f"{error.get('Code', 'Unknown')}: {error.get('Message', str(exc))}"


def list_all_buckets(s3_client: Any) -> list[str]:
    """Return all bucket names visible to the configured S3 client."""
    response = s3_client.list_buckets()
    return [bucket["Name"] for bucket in response.get("Buckets", [])]


def list_objects_in_bucket(bucket_name: str, s3_client: Any) -> list[dict[str, Any]]:
    """Return all objects in a bucket using ListObjectsV2 pagination."""
    objects: list[dict[str, Any]] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name):
        objects.extend(page.get("Contents", []))
    return objects


def download_file(
    s3_client: Any,
    bucket_name: str,
    object_name: str,
    local_file_path: str,
) -> None:
    """Download an object to a local file path."""
    s3_client.download_file(bucket_name, object_name, local_file_path)


def check_bucket_permissions(
    s3_client: Any,
    bucket_name: str,
    sample_key: str | None = None,
    test_write: bool = False,
) -> PermissionReport:
    """Check list/read/write/delete permissions for a bucket.

    By default this is read-only. Set ``test_write=True`` to create and
    immediately delete a temporary object for write/delete verification.
    """
    result: PermissionReport = {
        "list": False,
        "read": None,
        "write": None,
        "delete": None,
    }

    try:
        response = s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
        result["list"] = True
        if sample_key is None and response.get("Contents"):
            sample_key = response["Contents"][0]["Key"]
    except ClientError as exc:
        result["list"] = _client_error_message(exc)

    if sample_key is not None:
        try:
            response = s3_client.get_object(Bucket=bucket_name, Key=sample_key)
            response["Body"].close()
            result["read"] = True
        except ClientError as exc:
            result["read"] = _client_error_message(exc)

    if not test_write:
        return result

    probe_key = f".permission-check/{uuid.uuid4().hex}"
    probe_created = False
    try:
        s3_client.put_object(
            Bucket=bucket_name,
            Key=probe_key,
            Body=b"boto3 permission check",
        )
        probe_created = True
        result["write"] = True
    except ClientError as exc:
        result["write"] = _client_error_message(exc)
    finally:
        if probe_created:
            try:
                s3_client.delete_object(Bucket=bucket_name, Key=probe_key)
                result["delete"] = True
            except ClientError as exc:
                result["delete"] = _client_error_message(exc)
                result["undeleted_probe_key"] = probe_key

    return result


class S3OS:
    """A lightweight os-style access layer for a single S3 bucket.

    S3 has object keys rather than real directories. ``S3OS`` treats
    directories as key prefixes and always uses POSIX-style ``/`` separators.
    """

    def __init__(self, bucket_name: str, client: Any):
        if client is None:
            raise ValueError("client is required")
        self.bucket_name = bucket_name
        self.client = client

    @staticmethod
    def _add_delete_objects_content_md5(request: Any, **_: Any) -> None:
        """Add Content-MD5 for S3-compatible DeleteObjects requests."""
        if "Content-MD5" in request.headers or request.body is None:
            return

        body = request.body
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif isinstance(body, bytearray):
            body = bytes(body)
        elif not isinstance(body, bytes):
            position = body.tell()
            body = body.read()
            body.seek(position)

        try:
            digest = hashlib.md5(body, usedforsecurity=False).digest()
        except TypeError:
            digest = hashlib.md5(body).digest()
        request.headers["Content-MD5"] = base64.b64encode(digest).decode("ascii")

    def _enable_delete_objects_content_md5(self) -> None:
        self.client.meta.events.register_first(
            "before-sign.s3.DeleteObjects",
            self._add_delete_objects_content_md5,
            unique_id="s3os-delete-objects-content-md5",
        )

    @staticmethod
    def _key(path: str | bytes | object) -> str:
        if path is None:
            raise TypeError("path cannot be None")
        return str(path).replace("\\", "/").strip("/")

    def join(self, *parts: object) -> str:
        """Join path fragments with the S3 ``/`` separator."""
        cleaned = [self._key(part) for part in parts if str(part).strip("/\\")]
        return posixpath.join(*cleaned) if cleaned else ""

    def isfile(self, path: object) -> bool:
        key = self._key(path)
        if not key:
            return False
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def isdir(self, path: object) -> bool:
        key = self._key(path)
        prefix = f"{key}/" if key else ""
        response = self.client.list_objects_v2(
            Bucket=self.bucket_name,
            Prefix=prefix,
            MaxKeys=1,
        )
        return bool(
            response.get("KeyCount", 0)
            or response.get("Contents")
            or response.get("CommonPrefixes")
        )

    def exists(self, path: object) -> bool:
        return self.isfile(path) or self.isdir(path)

    def listdir(self, path: object = "") -> list[str]:
        """Return direct child file and directory names under a prefix."""
        key = self._key(path)
        prefix = f"{key}/" if key else ""
        names: set[str] = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket_name,
            Prefix=prefix,
            Delimiter="/",
        ):
            for item in page.get("Contents", []):
                relative = item["Key"][len(prefix) :]
                if relative:
                    names.add(relative)
            for item in page.get("CommonPrefixes", []):
                relative = item["Prefix"][len(prefix) :].rstrip("/")
                if relative:
                    names.add(relative)
        return sorted(names)

    def walk(
        self,
        top: object = "",
        topdown: bool = True,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        """Yield directory paths, child directories, and files below ``top``.

        The result follows ``os.walk`` conventions. When ``topdown`` is true,
        callers may modify the yielded directory list in place to prune the
        traversal.
        """
        top_key = self._key(top)

        def visit(root: str) -> Iterator[tuple[str, list[str], list[str]]]:
            prefix = f"{root}/" if root else ""
            directories: set[str] = set()
            files: set[str] = set()
            exists = not root
            paginator = self.client.get_paginator("list_objects_v2")

            for page in paginator.paginate(
                Bucket=self.bucket_name,
                Prefix=prefix,
                Delimiter="/",
            ):
                contents = page.get("Contents", [])
                common_prefixes = page.get("CommonPrefixes", [])
                exists = exists or bool(contents) or bool(common_prefixes)

                for item in contents:
                    relative = item["Key"][len(prefix) :]
                    if relative:
                        files.add(relative)
                for item in common_prefixes:
                    relative = item["Prefix"][len(prefix) :].rstrip("/")
                    if relative:
                        directories.add(relative)

            if not exists:
                return

            directory_names = sorted(directories)
            file_names = sorted(files)
            if topdown:
                yield root, directory_names, file_names
            for directory in directory_names:
                yield from visit(self.join(root, directory))
            if not topdown:
                yield root, directory_names, file_names

        yield from visit(top_key)

    def glob(self, pattern: object) -> list[str]:
        """Return sorted object keys matching a path-aware glob pattern.

        ``*``, ``?``, and character ranges match within one path component.
        ``**`` matches zero or more complete path components.
        """
        normalized_pattern = self._key(pattern)
        if not normalized_pattern:
            return []

        prefix_parts: list[str] = []
        for part in normalized_pattern.split("/"):
            if any(character in part for character in "*?["):
                break
            prefix_parts.append(part)

        prefix = "/".join(prefix_parts)
        if prefix_parts and len(prefix_parts) < len(normalized_pattern.split("/")):
            prefix += "/"

        matches: set[str] = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                if not key.endswith("/") and _glob_match(key, normalized_pattern):
                    matches.add(key)
        return sorted(matches)

    @overload
    def open(
        self,
        path: object,
        mode: Literal["r"],
        encoding: str = "utf-8",
    ) -> Iterator[TextIO]: ...

    @overload
    def open(
        self,
        path: object,
        mode: Literal["rb"] = "rb",
        encoding: str = "utf-8",
    ) -> Iterator[BinaryIO]: ...

    @overload
    def open(
        self,
        path: object,
        mode: Literal["w"],
        encoding: str = "utf-8",
    ) -> Iterator[TextIO]: ...

    @overload
    def open(
        self,
        path: object,
        mode: Literal["wb"],
        encoding: str = "utf-8",
    ) -> Iterator[BinaryIO]: ...

    @contextmanager
    def open(
        self,
        path: object,
        mode: OpenMode = "rb",
        encoding: str = "utf-8",
    ) -> Iterator[TextIO | BinaryIO]:
        """Open an S3 object for small buffered reads or writes.

        The whole object is buffered in memory. Prefer ``download``/``upload``
        for large files.
        """
        if mode not in {"r", "rb", "w", "wb"}:
            raise ValueError("mode must be one of 'r', 'rb', 'w', or 'wb'")

        key = self._key(path)
        if not key:
            raise ValueError("cannot open the bucket root as a file")

        if mode.startswith("r"):
            response = self.client.get_object(Bucket=self.bucket_name, Key=key)
            try:
                data = response["Body"].read()
            finally:
                response["Body"].close()
            stream: TextIO | BinaryIO
            stream = io.BytesIO(data) if "b" in mode else io.StringIO(data.decode(encoding))
            try:
                yield stream
            finally:
                stream.close()
            return

        stream = io.BytesIO() if "b" in mode else io.StringIO()
        try:
            yield stream
        except Exception:
            raise
        else:
            value = stream.getvalue()
            body = value if isinstance(value, bytes) else value.encode(encoding)
            self.client.put_object(Bucket=self.bucket_name, Key=key, Body=body)
        finally:
            stream.close()

    def remove(self, path: object) -> None:
        """Delete one object. This does not recursively delete a prefix."""
        key = self._key(path)
        if not key:
            raise ValueError("cannot delete the bucket root")
        self.client.delete_object(Bucket=self.bucket_name, Key=key)

    unlink = remove

    def copy(self, source: object, destination: object) -> None:
        """Copy one object within the bucket using boto3's managed transfer."""
        source_key = self._key(source)
        destination_key = self._key(destination)
        if not source_key or not destination_key:
            raise ValueError("source and destination must be object keys")
        if source_key == destination_key:
            raise ValueError("source and destination must be different")

        self.client.copy(
            {"Bucket": self.bucket_name, "Key": source_key},
            self.bucket_name,
            destination_key,
        )

    def move(self, source: object, destination: object) -> None:
        """Move one object by copying it, then deleting the source."""
        source_key = self._key(source)
        destination_key = self._key(destination)
        self.copy(source_key, destination_key)
        self.remove(source_key)

    def _supports_native_rename(self) -> bool:
        return callable(getattr(self.client, "rename_object", None))

    def _native_rename(self, source_key: str, destination_key: str) -> None:
        self.client.rename_object(
            Bucket=self.bucket_name,
            Key=destination_key,
            RenameSource=quote(source_key, safe="/"),
        )

    def _native_rename_pair(self, pair: tuple[str, str]) -> None:
        self._native_rename(*pair)

    def rename(
        self,
        source: object,
        destination: object,
        mode: RenameMode = "auto",
        max_workers: int = 16,
    ) -> None:
        """Rename one object or directory prefix.

        ``auto`` uses native ``RenameObject`` for S3 Express directory buckets
        and copy-then-delete elsewhere. ``native`` refuses to copy data when
        the provider does not support native rename. ``copy`` always uses the
        portable copy-then-delete implementation.
        """
        source_key = self._key(source)
        destination_key = self._key(destination)
        if mode not in {"auto", "native", "copy"}:
            raise ValueError("mode must be 'auto', 'native', or 'copy'")
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if not source_key or not destination_key:
            raise ValueError("source and destination must be object keys or prefixes")
        if source_key == destination_key:
            raise ValueError("source and destination must be different")

        is_directory_bucket = self.bucket_name.endswith("--x-s3")
        use_native = mode == "native" or (mode == "auto" and is_directory_bucket)
        if use_native and (not is_directory_bucket or not self._supports_native_rename()):
            raise NotImplementedError(
                "native rename requires an AWS S3 Express directory bucket "
                "and a recent boto3 client with RenameObject support"
            )

        if use_native:
            try:
                self._native_rename(source_key, destination_key)
                return
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code not in {"404", "NoSuchKey", "NotFound"}:
                    raise
        elif self.isfile(source_key):
            self.move(source_key, destination_key)
            return

        source_prefix = f"{source_key}/"
        destination_prefix = f"{destination_key}/"
        if destination_prefix.startswith(source_prefix):
            raise ValueError("destination cannot be inside the source prefix")

        if not use_native:
            self._enable_delete_objects_content_md5()
        found_source = False
        while True:
            response = self.client.list_objects_v2(
                Bucket=self.bucket_name,
                Prefix=source_prefix,
                MaxKeys=1000,
            )
            source_keys = [item["Key"] for item in response.get("Contents", [])]
            if not source_keys:
                break

            found_source = True
            rename_pairs: list[tuple[str, str]] = []
            for object_key in source_keys:
                relative_key = object_key[len(source_prefix) :]
                target_key = f"{destination_prefix}{relative_key}"
                if use_native:
                    rename_pairs.append((object_key, target_key))
                else:
                    self.copy(object_key, target_key)

            if use_native:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    list(executor.map(self._native_rename_pair, rename_pairs))

            if not use_native:
                batch = [{"Key": key} for key in source_keys]
                delete_response = self.client.delete_objects(
                    Bucket=self.bucket_name,
                    Delete={"Objects": batch, "Quiet": True},
                )
                errors = delete_response.get("Errors", [])
                if errors:
                    details = "; ".join(
                        f"{item.get('Key')}: {item.get('Code')} {item.get('Message', '')}"
                        for item in errors
                    )
                    raise RuntimeError(f"some source objects failed to delete: {details}")

        if not found_source:
            raise FileNotFoundError(
                f"no object or directory prefix found: s3://{self.bucket_name}/{source_key}"
            )

    def rmtree(self, path: object) -> int:
        """Recursively delete all objects under ``path/`` and return the count."""
        key = self._key(path)
        if not key:
            raise ValueError("cannot recursively delete the bucket root")

        self._enable_delete_objects_content_md5()
        prefix = f"{key}/"
        deleted_count = 0

        while True:
            response = self.client.list_objects_v2(
                Bucket=self.bucket_name,
                Prefix=prefix,
                MaxKeys=1000,
            )
            objects = [{"Key": item["Key"]} for item in response.get("Contents", [])]
            if not objects:
                break

            delete_response = self.client.delete_objects(
                Bucket=self.bucket_name,
                Delete={"Objects": objects, "Quiet": True},
            )
            errors = delete_response.get("Errors", [])
            if errors:
                details = "; ".join(
                    f"{item.get('Key')}: {item.get('Code')} {item.get('Message', '')}"
                    for item in errors
                )
                raise RuntimeError(f"some S3 objects failed to delete: {details}")

            deleted_count += len(objects)

        return deleted_count

    def download(self, path: object, local_file_path: str) -> None:
        self.client.download_file(self.bucket_name, self._key(path), local_file_path)

    def upload(self, local_file_path: str, path: object) -> None:
        self.client.upload_file(local_file_path, self.bucket_name, self._key(path))
