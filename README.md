# s3os

[![CI](https://github.com/alivecat05/s3os/actions/workflows/ci.yml/badge.svg)](https://github.com/alivecat05/s3os/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/github/license/alivecat05/s3os)](LICENSE)

Small os-style helpers for working with S3 buckets.

`s3os` wraps a boto3 S3 client with familiar file operations such as `listdir`,
`isfile`, `isdir`, `mkdir`, `walk`, `glob`, `open`, `copy`, `move`, `rename`, `remove`,
`rmtree`, `upload`, and `download`. It is
useful when your code treats an S3 bucket like a lightweight project filesystem
but you still want to keep direct control of the boto3 client and credentials.

## Why s3os?

- Tiny API surface on top of boto3.
- POSIX-style path handling for S3 object keys.
- Directory-like operations that understand S3 prefixes.
- Buffered `open()` for small text/binary objects.
- Safer recursive delete behavior: `rmtree("frames")` deletes `frames/`,
  not `frames_backup/`.
- Adds `Content-MD5` to `DeleteObjects` requests for S3-compatible providers
  that require it.


## Installation

Install from GitHub:

```bash
python -m pip install "s3os @ git+https://github.com/alivecat05/s3os.git"
```

For local development:

```bash
git clone https://github.com/alivecat05/s3os.git
cd s3os
python -m pip install -e ".[dev]"
python -m pytest
```

## Quick Start

```python
import boto3
from botocore.client import Config
from s3os import S3OS

client = boto3.client(
    "s3",
    aws_access_key_id="...",
    aws_secret_access_key="...",
    endpoint_url="https://s3.example.com",
    config=Config(
        s3={"addressing_style": "path"},
        signature_version="s3v4",
    ),
)

s3 = S3OS("my-bucket", client)

print(s3.listdir("datasets"))
print(s3.exists("datasets/train.jsonl"))

with s3.open("notes/hello.txt", "w") as file:
    file.write("hello from s3os\n")

with s3.open("notes/hello.txt", "r") as file:
    print(file.read())
```

See [examples/basic_usage.py](examples/basic_usage.py) for an environment
variable based example. It is read-only by default; set
`S3OS_RUN_WRITE_EXAMPLE=1` to run its write/read demonstration.

## API

### `S3OS(bucket_name, client)`

Creates an os-style helper bound to one bucket and one boto3-compatible S3
client.

### Path Helpers

```python
s3.join("folder", "child", "file.txt")
s3.exists("folder/file.txt")
s3.isfile("folder/file.txt")
s3.isdir("folder")
s3.listdir("folder")
s3.mkdir("folder/new", parents=True, exist_ok=True)
```

S3 keys always use `/`, even on Windows.

`mkdir()` creates a zero-byte directory marker such as `folder/new/`. S3 does
not have real directories; use `parents=True` to create missing parent markers
and `exist_ok=True` to make the operation idempotent.

### Walk and Glob

```python
for root, directories, files in s3.walk("datasets"):
    print(root, directories, files)

jsonl_files = s3.glob("datasets/**/*.jsonl")
checkpoints = s3.glob("models/checkpoint-??.bin")
```

`walk()` follows `os.walk()` conventions, including top-down pruning by
modifying the yielded directory list. In `glob()`, `*`, `?`, and character
ranges match within one path component, while `**` matches recursively.

### File IO

```python
with s3.open("config.json", "r") as file:
    text = file.read()

with s3.open("data.bin", "wb") as file:
    file.write(b"...")
```

`open()` buffers the whole object in memory. For large objects, use:

```python
s3.download("large/file.zip", "file.zip")
s3.upload("file.zip", "large/file.zip")
```

### Delete

```python
s3.remove("folder/file.txt")
deleted_count = s3.rmtree("folder")
```

`remove()` deletes exactly one object. `rmtree()` deletes objects below the
`folder/` prefix and refuses to delete the bucket root.

### Copy, Move, and Rename

```python
s3.copy("models/latest.bin", "models/archive/v1.bin")
s3.move("logs/current.jsonl", "logs/processed/current.jsonl")
s3.rename("models/draft.bin", "models/final.bin")
s3.rename("datasets/raw", "datasets/processed")
s3.rename("large/model.bin", "archive/model.bin", mode="native")
s3.rename("large-dataset", "archive/dataset", mode="native", max_workers=32)
```

`copy()` and `move()` operate on one object within the bound bucket. `rename()`
accepts either one object or a directory-like prefix and preserves relative
child keys when renaming a prefix. Existing destinations are overwritten,
matching S3 behavior.

`copy()` uses boto3's managed transfer, including multipart copy for large
objects; object payloads are not downloaded to the local machine. `move()`
deletes the source only after its copy succeeds.

A directory `rename()` processes at most 1,000 keys at a time, so memory use
does not grow with the total number of objects. Each batch is deleted only after
every object in that batch has copied successfully. S3 has no atomic rename: if
a later batch fails, earlier batches remain at the destination and unprocessed
objects remain at the source. Calling `rename()` again resumes naturally from
the remaining source objects.

`rename()` supports three strategies:

- `mode="auto"` (default): use native `RenameObject` for AWS S3 Express
  directory buckets and copy-then-delete for other providers. If boto3 is too
  old to expose `RenameObject`, it raises instead of silently copying data.
- `mode="native"`: require native rename and raise `NotImplementedError`
  instead of copying a large object when native rename is unavailable.
- `mode="copy"`: explicitly use the portable copy-then-delete implementation.

Native rename is currently an AWS S3 Express One Zone feature. For a 48 GB
object in an S3 Express directory bucket, it changes the key without copying
48 GB. Regular AWS S3 buckets, MinIO, Cloudflare R2, and most S3-compatible
providers do not expose a standard native rename operation.

For a directory prefix in S3 Express, `rename()` lists keys in bounded pages and
runs native metadata renames concurrently. `max_workers` defaults to 16 and can
be tuned for the account's request-rate limits. Object sizes do not affect the
amount of data moved because native rename does not copy object payloads.

### Permission Check

```python
from s3os import check_bucket_permissions

print(check_bucket_permissions(client, "my-bucket"))
print(check_bucket_permissions(client, "my-bucket", test_write=True))
```

By default this is read-only. `test_write=True` creates a temporary object under
`.permission-check/` and then deletes it.

### Standalone Helpers

```python
from s3os import download_file, list_all_buckets, list_objects_in_bucket

bucket_names = list_all_buckets(client)
objects = list_objects_in_bucket("my-bucket", client)
download_file(client, "my-bucket", "data.csv", "data.csv")
```

Object listing follows all `ListObjectsV2` pages. Errors from boto3 are allowed
to propagate so callers can handle provider-specific error codes.

## S3-Compatible Storage Notes

Many S3-compatible services require path-style addressing and Signature V4:

```python
Config(
    s3={"addressing_style": "path"},
    signature_version="s3v4",
)
```

`s3os` keeps client construction outside the library so you can configure AWS
S3, MinIO, Ceph, Cloudflare R2, or other compatible providers in the standard
boto3 way.

## Status

This project is alpha. The core API is intentionally small, but names and return
values may still change before a stable release.

S3 does not have real directories, atomic rename, file locking, or POSIX
permissions. Directory methods in this project interpret keys ending in `/` as
prefixes; they do not emulate those missing filesystem guarantees.

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python -m pip install build twine
python -m build
python -m twine check dist/*
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development expectations and
[SECURITY.md](SECURITY.md) for responsible vulnerability reporting.

## License

Apache-2.0
