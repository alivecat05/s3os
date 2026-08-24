import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.client import Config

# Use the HTTP/LFS upload path. Files are staged locally so Hugging Face can
# hash them without reading every object from S3 a second time.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# This compute environment cannot route directly to huggingface.co. Match the
# other upload scripts in this repository by using the Hugging Face mirror by
# default, while still allowing callers to select another endpoint explicitly.
HF_ENDPOINT = (os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com").rstrip(
    "/"
)
# hf-mirror.org is not resolvable on this worker. Some old shell settings use
# that hostname; normalize them to the working mirror hostname.
if urlsplit(HF_ENDPOINT).hostname == "hf-mirror.org":
    HF_ENDPOINT = "https://hf-mirror.com"
os.environ["HF_ENDPOINT"] = HF_ENDPOINT

from huggingface_hub import CommitOperationAdd, HfApi, RepoFile
from huggingface_hub import lfs as hf_lfs
from huggingface_hub.errors import EntryNotFoundError, HfHubHTTPError


# The mirror occasionally returns multipart completion URLs on
# `hf-mirror.org`, even when the API endpoint is `hf-mirror.com`. Rewrite only
# that mirror hostname; signed AWS part-upload URLs must remain untouched.
_fix_hf_endpoint_in_url = hf_lfs.fix_hf_endpoint_in_url
_reported_mirror_rewrite = False


def fix_mirror_url(url, endpoint=None):
    global _reported_mirror_rewrite

    fixed_url = _fix_hf_endpoint_in_url(url, endpoint)
    parsed = urlsplit(fixed_url)
    if parsed.hostname != "hf-mirror.org":
        return fixed_url

    configured = urlsplit(HF_ENDPOINT)
    if configured.hostname != "hf-mirror.com":
        return fixed_url

    if not _reported_mirror_rewrite:
        print(
            "Rewriting unresolvable hf-mirror.org callback to "
            "hf-mirror.com.",
            flush=True,
        )
        _reported_mirror_rewrite = True

    netloc = configured.netloc
    return urlunsplit(parsed._replace(scheme=configured.scheme, netloc=netloc))


hf_lfs.fix_hf_endpoint_in_url = fix_mirror_url


# ============================================================
# 配置
# ============================================================

BUCKET = "ace_data0"
PREFIX = "data_sg/"
TAKE_START = 0
TAKE_END = 467
BATCH_SIZE = int(os.environ.get("B2HF_BATCH_SIZE", "100"))
S3_DOWNLOAD_THREADS = int(os.environ.get("B2HF_S3_THREADS", "8"))
HF_UPLOAD_THREADS = int(os.environ.get("B2HF_HF_THREADS", "4"))
STAGING_DIR = os.environ.get("B2HF_STAGING_DIR") or None
REPO_ID = "ACERobotics/ACE-Data-0"
PATH_IN_REPO = "Room-scale"
CHECKPOINT_PATH = Path(__file__).with_name(".bucket2hf-uploaded.txt")

if min(BATCH_SIZE, S3_DOWNLOAD_THREADS, HF_UPLOAD_THREADS) < 1:
    raise ValueError("Batch size and thread counts must all be at least 1.")

AWS_ACCESS_KEY_ID = "019FE23D0767764488DD0D9DD2F91D1F"
AWS_SECRET_ACCESS_KEY = "019FE23D07677632A2EF174667B94044"

if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
    raise RuntimeError(
        "Please set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
        "before running this script."
    )

REGION = "cn-sh-01b"
ENDPOINT_URL = "https://aoss-internal.cn-sh-01b.sensecoreapi-oss.cn"
S3_ENDPOINT_HOST = "aoss-internal.cn-sh-01b.sensecoreapi-oss.cn"


def add_no_proxy(host):
    """Ensure the private S3 endpoint bypasses the shell HTTP proxy."""
    for variable in ("NO_PROXY", "no_proxy"):
        entries = [
            entry.strip()
            for entry in os.environ.get(variable, "").split(",")
            if entry.strip()
        ]
        if host not in entries:
            entries.append(host)
        os.environ[variable] = ",".join(entries)


add_no_proxy(S3_ENDPOINT_HOST)


# ============================================================
# boto3：用于遍历 S3 对象
# ============================================================

botocore_config = Config(
    region_name=REGION,
    signature_version="s3v4",
    connect_timeout=10,
    read_timeout=60,
    max_pool_connections=max(10, S3_DOWNLOAD_THREADS),
    # Hugging Face needs the shell proxy, but this private S3 endpoint must
    # be reached directly.
    proxies={},
    s3={
        "addressing_style": "path",
    },
    retries={
        "max_attempts": 3,
        "mode": "adaptive",
    },
)

client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    endpoint_url=ENDPOINT_URL,
    region_name=REGION,
    config=botocore_config,
)


# ============================================================
# Hugging Face
# ============================================================

api = HfApi(endpoint=HF_ENDPOINT)

print(f"Hugging Face endpoint: {api.endpoint}", flush=True)
print(f"Concurrent S3 downloads: {S3_DOWNLOAD_THREADS}", flush=True)
print(f"Concurrent Hugging Face file uploads: {HF_UPLOAD_THREADS}", flush=True)

# 检查仓库和写权限；短暂的网络抖动不会让程序立即退出。
for attempt in range(1, 4):
    try:
        repo_info = api.repo_info(
            repo_id=REPO_ID,
            repo_type="dataset",
        )
        print(f"HF repository: {repo_info.id}", flush=True)
        break
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(
            f"HF connection attempt {attempt}/3 failed: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        if attempt == 3:
            raise RuntimeError(
                f"Cannot reach Hugging Face endpoint {api.endpoint}. "
                "Check the machine's network route or HTTPS proxy."
            ) from exc
        wait_seconds = attempt * 10
        print(f"Retrying in {wait_seconds} seconds...", flush=True)
        time.sleep(wait_seconds)


# ============================================================
# S3 -> Hugging Face
# ============================================================

paginator = client.get_paginator("list_objects_v2")

uploaded_count = 0
uploaded_bytes = 0
skipped_count = 0


def load_checkpoint():
    if not CHECKPOINT_PATH.exists():
        return set()
    return {
        line.strip()
        for line in CHECKPOINT_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_checkpoint(paths):
    with CHECKPOINT_PATH.open("a", encoding="utf-8") as checkpoint:
        for path in paths:
            checkpoint.write(f"{path}\n")
        checkpoint.flush()
        os.fsync(checkpoint.fileno())


def get_remote_paths(take_name):
    remote_folder = f"{PATH_IN_REPO}/{take_name}"
    print(f"Checking remote folder: {remote_folder}", flush=True)

    for attempt in range(1, 4):
        try:
            paths = {
                entry.path
                for entry in api.list_repo_tree(
                    repo_id=REPO_ID,
                    path_in_repo=remote_folder,
                    recursive=True,
                    repo_type="dataset",
                )
                if isinstance(entry, RepoFile)
            }
            print(f"Remote files found: {len(paths)}", flush=True)
            return paths
        except EntryNotFoundError:
            print("Remote folder does not exist yet.", flush=True)
            return set()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(
                f"Remote listing attempt {attempt}/3 failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            if attempt == 3:
                raise
            time.sleep(attempt * 10)


def rate_limit_wait_seconds(exc):
    if not isinstance(exc, HfHubHTTPError):
        return None
    response = getattr(exc, "response", None)
    if response is None or response.status_code != 429:
        return None

    try:
        retry_after = int(response.headers.get("Retry-After", "0"))
    except (TypeError, ValueError):
        retry_after = 0

    # The repository commit limit has a one-hour window. Its response can also
    # contain a shorter generic API Retry-After value, which is insufficient.
    if "repository commits" in str(exc).lower():
        retry_after = max(retry_after, 3600)
    return max(retry_after, 60) + 5


def download_to_path(item, destination):
    for attempt in range(1, 4):
        try:
            response = client.get_object(Bucket=BUCKET, Key=item["key"])
            body = response["Body"]
            try:
                with destination.open("wb") as target:
                    shutil.copyfileobj(body, target, length=8 * 1024 * 1024)
            finally:
                body.close()

            actual_size = destination.stat().st_size
            if actual_size != item["size"]:
                raise IOError(
                    f"Incomplete S3 download for {item['s3_path']}: "
                    f"expected {item['size']} bytes, got {actual_size}"
                )
            return actual_size
        except Exception as exc:
            if attempt == 3:
                raise
            print(
                f"S3 download attempt {attempt}/3 failed for "
                f"{item['s3_path']}: {type(exc).__name__}: {exc}",
                flush=True,
            )
            time.sleep(attempt * 5)


def make_commit_operation(item_and_local_path):
    item, local_path = item_and_local_path
    return CommitOperationAdd(
        path_in_repo=item["path_in_repo"],
        path_or_fileobj=local_path,
    )


def stage_batch(batch, staging_dir):
    local_paths = [
        Path(staging_dir) / f"{index:06d}.upload"
        for index in range(len(batch))
    ]
    total_bytes = sum(item["size"] for item in batch)
    completed_files = 0
    completed_bytes = 0

    print(
        f"Downloading {len(batch)} files from S3 "
        f"({total_bytes / 1024**3:.3f} GiB)...",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=S3_DOWNLOAD_THREADS) as executor:
        futures = {
            executor.submit(download_to_path, item, local_path): item
            for item, local_path in zip(batch, local_paths)
        }
        for future in as_completed(futures):
            item = futures[future]
            completed_bytes += future.result()
            completed_files += 1
            print(
                f"S3 staged: {completed_files}/{len(batch)} files, "
                f"{completed_bytes / 1024**3:.3f}/"
                f"{total_bytes / 1024**3:.3f} GiB "
                f"({item['s3_path']})",
                flush=True,
            )

    return local_paths


def upload_batch(batch, batch_number):
    global uploaded_count, uploaded_bytes

    print(
        f"\nPreparing batch {batch_number}: {len(batch)} files",
        flush=True,
    )
    with tempfile.TemporaryDirectory(
        prefix=f"bucket2hf-{batch_number}-",
        dir=STAGING_DIR,
    ) as staging_dir:
        local_paths = stage_batch(batch, staging_dir)

        for attempt in range(1, 6):
            try:
                print(
                    f"Hashing and uploading batch {batch_number} "
                    f"(attempt {attempt}/5)...",
                    flush=True,
                )
                with ThreadPoolExecutor(
                    max_workers=HF_UPLOAD_THREADS
                ) as executor:
                    operations = list(
                        executor.map(
                            make_commit_operation,
                            zip(batch, local_paths),
                        )
                    )
                result = api.create_commit(
                    repo_id=REPO_ID,
                    repo_type="dataset",
                    operations=operations,
                    commit_message=(
                        f"Upload batch {batch_number} "
                        f"({len(batch)} files)"
                    ),
                    num_threads=HF_UPLOAD_THREADS,
                )

                paths = [item["path_in_repo"] for item in batch]
                save_checkpoint(paths)
                uploaded_count += len(batch)
                uploaded_bytes += sum(item["size"] for item in batch)
                print(
                    f"Batch committed successfully: {result.commit_url}",
                    flush=True,
                )
                return

            except KeyboardInterrupt:
                print(
                    "\nUpload interrupted. The last successful batch is saved; "
                    "rerun the script to resume.",
                    flush=True,
                )
                raise
            except Exception as exc:
                wait_seconds = rate_limit_wait_seconds(exc)
                print(
                    f"Batch {batch_number} attempt {attempt}/5 failed:\n"
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                if attempt == 5:
                    raise RuntimeError(
                        f"Failed to upload batch {batch_number}. "
                        "Rerun the script to resume from its checkpoint."
                    ) from exc

                if wait_seconds is None:
                    wait_seconds = attempt * 30
                print(
                    f"Retrying this batch in {wait_seconds} seconds. "
                    "Press Ctrl+C to stop safely and rerun later.",
                    flush=True,
                )
                time.sleep(wait_seconds)


checkpoint_paths = load_checkpoint()
print(
    f"Resume checkpoint: {len(checkpoint_paths)} completed files "
    f"({CHECKPOINT_PATH})",
    flush=True,
)

print(
    f"Selected S3 folders: take-{TAKE_START:06d} through "
    f"take-{TAKE_END:06d}",
    flush=True,
)

for take_index in range(TAKE_START, TAKE_END + 1):
    take_name = f"take-{take_index:06d}"
    take_prefix = f"{PREFIX}{take_name}/"
    completed_paths = checkpoint_paths | get_remote_paths(take_name)
    batch = []
    batch_number = 0

    print(f"Listing S3 folder: s3://{BUCKET}/{take_prefix}", flush=True)

    for page in paginator.paginate(
        Bucket=BUCKET,
        Prefix=take_prefix,
        PaginationConfig={
            "PageSize": 1000,
        },
    ):
        for item in page.get("Contents", []):
            key = item["Key"]
            size = item.get("Size", 0)

            # 跳过 S3 目录占位对象
            if key.endswith("/"):
                continue

            # data_sg/take-000000/foo.mp4 -> take-000000/foo.mp4
            relative_path = key[len(PREFIX):].lstrip("/")

            if not relative_path:
                continue

            # 最终路径：Room-scale/take-000000/foo.mp4
            path_in_repo = f"{PATH_IN_REPO}/{relative_path}"
            s3_path = f"{BUCKET}/{key}"

            if path_in_repo in completed_paths:
                skipped_count += 1
                continue

            batch.append(
                {
                    "key": key,
                    "s3_path": s3_path,
                    "path_in_repo": path_in_repo,
                    "size": size,
                }
            )

            if len(batch) >= BATCH_SIZE:
                batch_number += 1
                upload_batch(batch, f"{take_name}-{batch_number:05d}")
                paths = {item["path_in_repo"] for item in batch}
                checkpoint_paths.update(paths)
                completed_paths.update(paths)
                batch = []

    if batch:
        batch_number += 1
        upload_batch(batch, f"{take_name}-{batch_number:05d}")
        paths = {item["path_in_repo"] for item in batch}
        checkpoint_paths.update(paths)
        completed_paths.update(paths)

print(
    "\nUpload completed.\n"
    f"Files uploaded: {uploaded_count}\n"
    f"Files skipped (already uploaded): {skipped_count}\n"
    f"Total size: {uploaded_bytes / 1024**3:.3f} GiB",
    flush=True,
)
