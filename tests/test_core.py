from __future__ import annotations

import base64
import hashlib
import io

import pytest
from botocore.exceptions import ClientError

from s3os import S3OS, check_bucket_permissions, list_all_buckets, list_objects_in_bucket


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        yield from self.pages


class FakeEvents:
    def __init__(self):
        self.registrations = []

    def register_first(self, event_name, handler, unique_id):
        self.registrations.append((event_name, handler, unique_id))


class FakeMeta:
    def __init__(self):
        self.events = FakeEvents()


class FakeClient:
    def __init__(self):
        self.meta = FakeMeta()
        self.objects = {}
        self.deleted = []
        self.downloads = []
        self.uploads = []
        self.copies = []
        self.copy_error = None
        self.native_renames = []
        self.native_rename_error = None
        self.delete_batches = []
        self.list_calls = []
        self.paginator = FakePaginator([])
        self.list_pages = []

    def list_buckets(self):
        return {"Buckets": [{"Name": "alpha"}, {"Name": "beta"}]}

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.list_pages:
            return self.list_pages.pop(0)
        prefix = kwargs.get("Prefix", "")
        contents = [{"Key": key} for key in self.objects if key.startswith(prefix)]
        max_keys = kwargs.get("MaxKeys")
        if max_keys is not None:
            contents = contents[:max_keys]
        return {"KeyCount": len(contents), "Contents": contents}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self.paginator

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not found"}},
                "HeadObject",
            )
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def delete_objects(self, Bucket, Delete):
        self.delete_batches.append([item["Key"] for item in Delete["Objects"]])
        for item in Delete["Objects"]:
            self.deleted.append(item["Key"])
            self.objects.pop(item["Key"], None)
        return {}

    def copy(self, CopySource, Bucket, Key):
        if self.copy_error is not None:
            raise self.copy_error
        source_key = CopySource["Key"]
        self.copies.append((CopySource, Bucket, Key))
        self.objects[Key] = self.objects[source_key]

    def rename_object(self, Bucket, Key, RenameSource):
        if self.native_rename_error is not None:
            raise self.native_rename_error
        source_key = RenameSource.replace("%20", " ")
        if source_key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                "RenameObject",
            )
        self.native_renames.append((Bucket, Key, RenameSource))
        self.objects[Key] = self.objects.pop(source_key)

    def download_file(self, bucket_name, object_name, local_file_path):
        self.downloads.append((bucket_name, object_name, local_file_path))

    def upload_file(self, local_file_path, bucket_name, object_name):
        self.uploads.append((local_file_path, bucket_name, object_name))


def test_join_normalizes_s3_paths():
    s3 = S3OS("bucket", FakeClient())

    assert s3.join("/foo/", r"bar\baz", "file.txt") == "foo/bar/baz/file.txt"
    assert s3.join("", "/", "foo") == "foo"
    assert s3.join("", "/") == ""


def test_listdir_returns_direct_children_only():
    client = FakeClient()
    client.paginator = FakePaginator(
        [
            {
                "Contents": [{"Key": "data/file.txt"}],
                "CommonPrefixes": [{"Prefix": "data/images/"}],
            },
            {
                "Contents": [{"Key": "data/readme.md"}],
                "CommonPrefixes": [{"Prefix": "data/logs/"}],
            },
        ]
    )
    s3 = S3OS("bucket", client)

    assert s3.listdir("data") == ["file.txt", "images", "logs", "readme.md"]
    assert client.paginator.calls[0]["Delimiter"] == "/"
    assert client.paginator.calls[0]["Prefix"] == "data/"


def test_walk_yields_sorted_tree_and_supports_pruning():
    class WalkPaginator:
        def __init__(self):
            self.calls = []

        def paginate(self, **kwargs):
            self.calls.append(kwargs)
            prefix = kwargs["Prefix"]
            pages = {
                "data/": [
                    {
                        "Contents": [{"Key": "data/readme.md"}],
                        "CommonPrefixes": [
                            {"Prefix": "data/images/"},
                            {"Prefix": "data/private/"},
                        ],
                    }
                ],
                "data/images/": [{"Contents": [{"Key": "data/images/a.png"}]}],
                "data/private/": [{"Contents": [{"Key": "data/private/key.txt"}]}],
            }
            yield from pages.get(prefix, [])

    client = FakeClient()
    client.paginator = WalkPaginator()
    s3 = S3OS("bucket", client)
    walker = s3.walk("data")

    root, directories, files = next(walker)
    directories.remove("private")

    assert (root, directories, files) == ("data", ["images"], ["readme.md"])
    assert list(walker) == [("data/images", [], ["a.png"])]


def test_walk_returns_nothing_for_a_missing_prefix():
    s3 = S3OS("bucket", FakeClient())

    assert list(s3.walk("missing")) == []


def test_walk_can_yield_children_before_their_parent():
    class WalkPaginator:
        def paginate(self, **kwargs):
            if kwargs["Prefix"] == "logs/":
                yield {"CommonPrefixes": [{"Prefix": "logs/2026/"}]}
            elif kwargs["Prefix"] == "logs/2026/":
                yield {"Contents": [{"Key": "logs/2026/app.log"}]}

    client = FakeClient()
    client.paginator = WalkPaginator()
    s3 = S3OS("bucket", client)

    assert list(s3.walk("logs", topdown=False)) == [
        ("logs/2026", [], ["app.log"]),
        ("logs", ["2026"], []),
    ]


def test_glob_matches_path_components_and_recursive_patterns():
    client = FakeClient()
    client.paginator = FakePaginator(
        [
            {
                "Contents": [
                    {"Key": "datasets/train.jsonl"},
                    {"Key": "datasets/2026/part-01.jsonl"},
                    {"Key": "datasets/2026/part-02.csv"},
                    {"Key": "datasets/archive/"},
                ]
            }
        ]
    )
    s3 = S3OS("bucket", client)

    assert s3.glob("datasets/**/*.jsonl") == [
        "datasets/2026/part-01.jsonl",
        "datasets/train.jsonl",
    ]
    assert client.paginator.calls == [{"Bucket": "bucket", "Prefix": "datasets/"}]


def test_open_reads_and_writes_text_objects():
    client = FakeClient()
    client.objects["config.json"] = b'{"ok": true}'
    s3 = S3OS("bucket", client)

    with s3.open("config.json", "r") as handle:
        assert handle.read() == '{"ok": true}'

    with s3.open("notes.txt", "w") as handle:
        handle.write("hello")

    assert client.objects["notes.txt"] == b"hello"


def test_exists_distinguishes_files_and_prefixes():
    client = FakeClient()
    client.objects = {"data/file.txt": b"", "data/nested/file.txt": b""}
    s3 = S3OS("bucket", client)

    assert s3.isfile("data/file.txt") is True
    assert s3.isdir("data") is True
    assert s3.exists("missing") is False


def test_mkdir_creates_a_directory_marker():
    client = FakeClient()
    s3 = S3OS("bucket", client)

    s3.mkdir("datasets")

    assert client.objects["datasets/"] == b""
    assert s3.isdir("datasets") is True


def test_mkdir_can_create_missing_parents_and_is_idempotent():
    client = FakeClient()
    s3 = S3OS("bucket", client)

    s3.mkdir("a/b/c", parents=True)
    s3.mkdir("a/b/c", parents=True, exist_ok=True)

    assert set(client.objects) == {"a/", "a/b/", "a/b/c/"}


def test_mkdir_requires_existing_parent_by_default():
    s3 = S3OS("bucket", FakeClient())

    with pytest.raises(FileNotFoundError, match="parent directory"):
        s3.mkdir("a/b")


def test_mkdir_rejects_existing_directory_without_exist_ok():
    client = FakeClient()
    client.objects["data/"] = b""
    s3 = S3OS("bucket", client)

    with pytest.raises(FileExistsError, match="already exists"):
        s3.mkdir("data")


def test_isdir_supports_providers_that_omit_key_count():
    client = FakeClient()
    client.list_pages = [{"Contents": [{"Key": "data/file.txt"}]}]
    s3 = S3OS("bucket", client)

    assert s3.isdir("data") is True


def test_rmtree_deletes_only_children_under_slash_prefix():
    client = FakeClient()
    client.objects = {
        "frames/a.png": b"",
        "frames/nested/b.png": b"",
        "frames_backup/c.png": b"",
    }
    client.list_pages = [
        {
            "Contents": [
                {"Key": "frames/a.png"},
                {"Key": "frames/nested/b.png"},
            ]
        },
        {},
    ]
    s3 = S3OS("bucket", client)

    assert s3.rmtree("frames") == 2
    assert client.deleted == ["frames/a.png", "frames/nested/b.png"]
    assert "frames_backup/c.png" in client.objects
    assert client.meta.events.registrations[0][0] == "before-sign.s3.DeleteObjects"


def test_rmtree_rejects_bucket_root():
    s3 = S3OS("bucket", FakeClient())

    with pytest.raises(ValueError, match="bucket root"):
        s3.rmtree("/")


def test_copy_and_move_objects_within_the_bucket():
    client = FakeClient()
    client.objects["models/current.bin"] = b"model"
    s3 = S3OS("bucket", client)

    s3.copy("models/current.bin", "models/archive/v1.bin")
    s3.move("models/current.bin", "models/current-v2.bin")

    assert client.objects["models/archive/v1.bin"] == b"model"
    assert client.objects["models/current-v2.bin"] == b"model"
    assert "models/current.bin" not in client.objects
    assert client.deleted == ["models/current.bin"]


def test_move_keeps_source_when_copy_fails():
    client = FakeClient()
    client.objects["source.txt"] = b"safe"
    client.copy_error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
        "CopyObject",
    )
    s3 = S3OS("bucket", client)

    with pytest.raises(ClientError):
        s3.move("source.txt", "destination.txt")

    assert client.objects["source.txt"] == b"safe"
    assert client.deleted == []


def test_rename_moves_an_object_to_a_new_key():
    client = FakeClient()
    client.objects["logs/current.log"] = b"log"
    s3 = S3OS("bucket", client)

    s3.rename("logs/current.log", "logs/archive/2026-08-14.log")

    assert client.objects["logs/archive/2026-08-14.log"] == b"log"
    assert "logs/current.log" not in client.objects
    assert client.deleted == ["logs/current.log"]


def test_s3_express_rename_uses_native_operation_without_copying():
    client = FakeClient()
    client.objects["large files/model 48g.bin"] = b"metadata-only-test"
    s3 = S3OS("models--usw2-az1--x-s3", client)

    s3.rename("large files/model 48g.bin", "archive/model 48g.bin")

    assert client.native_renames == [
        (
            "models--usw2-az1--x-s3",
            "archive/model 48g.bin",
            "large%20files/model%2048g.bin",
        )
    ]
    assert client.copies == []
    assert client.deleted == []
    assert client.objects["archive/model 48g.bin"] == b"metadata-only-test"


def test_native_mode_refuses_to_copy_on_a_regular_bucket():
    client = FakeClient()
    client.objects["large.bin"] = b"large"
    s3 = S3OS("regular-bucket", client)

    with pytest.raises(NotImplementedError, match="S3 Express"):
        s3.rename("large.bin", "renamed.bin", mode="native")

    assert client.copies == []
    assert client.objects["large.bin"] == b"large"


def test_auto_mode_does_not_copy_when_s3_express_client_is_too_old():
    client = FakeClient()
    client.rename_object = None
    client.objects["large.bin"] = b"large"
    s3 = S3OS("models--usw2-az1--x-s3", client)

    with pytest.raises(NotImplementedError, match="recent boto3"):
        s3.rename("large.bin", "renamed.bin")

    assert client.copies == []
    assert client.objects["large.bin"] == b"large"


def test_copy_mode_can_be_requested_explicitly_for_s3_express():
    client = FakeClient()
    client.objects["source.txt"] = b"value"
    s3 = S3OS("models--usw2-az1--x-s3", client)

    s3.rename("source.txt", "destination.txt", mode="copy")

    assert client.native_renames == []
    assert client.copies
    assert client.deleted == ["source.txt"]


def test_s3_express_directory_rename_uses_native_operation_per_object():
    client = FakeClient()
    client.objects = {
        "source/a.txt": b"a",
        "source/nested/b.txt": b"b",
    }
    s3 = S3OS("data--usw2-az1--x-s3", client)

    s3.rename("source", "destination")

    assert len(client.native_renames) == 2
    assert client.copies == []
    assert client.delete_batches == []
    assert client.objects == {
        "destination/a.txt": b"a",
        "destination/nested/b.txt": b"b",
    }


def test_s3_express_directory_rename_accepts_a_worker_limit():
    client = FakeClient()
    client.objects = {f"source/{index}.txt": b"value" for index in range(20)}
    s3 = S3OS("data--usw2-az1--x-s3", client)

    s3.rename("source", "destination", max_workers=4)

    assert len(client.native_renames) == 20
    assert all(key.startswith("destination/") for key in client.objects)


def test_rename_rejects_an_unknown_mode():
    s3 = S3OS("bucket", FakeClient())

    with pytest.raises(ValueError, match="mode must be"):
        s3.rename("source", "destination", mode="fast")


def test_rename_rejects_an_invalid_worker_limit():
    s3 = S3OS("bucket", FakeClient())

    with pytest.raises(ValueError, match="max_workers"):
        s3.rename("source", "destination", max_workers=0)


def test_rename_moves_a_directory_prefix_after_copying_all_objects():
    client = FakeClient()
    client.objects = {
        "datasets/raw/a.jsonl": b"a",
        "datasets/raw/nested/b.jsonl": b"b",
        "datasets/raw_backup/keep.jsonl": b"keep",
    }
    s3 = S3OS("bucket", client)

    s3.rename("datasets/raw", "datasets/processed")

    assert client.objects["datasets/processed/a.jsonl"] == b"a"
    assert client.objects["datasets/processed/nested/b.jsonl"] == b"b"
    assert client.objects["datasets/raw_backup/keep.jsonl"] == b"keep"
    assert "datasets/raw/a.jsonl" not in client.objects
    assert "datasets/raw/nested/b.jsonl" not in client.objects
    assert client.list_calls[-1] == {
        "Bucket": "bucket",
        "Prefix": "datasets/raw/",
        "MaxKeys": 1000,
    }


def test_directory_rename_processes_large_prefixes_in_bounded_batches():
    client = FakeClient()
    client.objects = {
        f"source/item-{index:04}.txt": str(index).encode() for index in range(1001)
    }
    s3 = S3OS("bucket", client)

    s3.rename("source", "destination")

    assert [len(batch) for batch in client.delete_batches] == [1000, 1]
    assert len(client.objects) == 1001
    assert all(key.startswith("destination/") for key in client.objects)
    assert all(call.get("MaxKeys") == 1000 for call in client.list_calls)


def test_directory_rename_keeps_sources_when_a_copy_fails():
    client = FakeClient()
    client.objects = {
        "source/a.txt": b"a",
        "source/b.txt": b"b",
    }
    client.copy_error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
        "CopyObject",
    )
    s3 = S3OS("bucket", client)

    with pytest.raises(ClientError):
        s3.rename("source", "destination")

    assert client.objects["source/a.txt"] == b"a"
    assert client.objects["source/b.txt"] == b"b"
    assert client.deleted == []


def test_rename_rejects_a_destination_inside_the_source_prefix():
    client = FakeClient()
    client.paginator = FakePaginator([])
    s3 = S3OS("bucket", client)

    with pytest.raises(ValueError, match="inside the source prefix"):
        s3.rename("datasets", "datasets/archive")


def test_rename_reports_a_missing_source_clearly():
    client = FakeClient()
    s3 = S3OS("bucket", client)

    with pytest.raises(FileNotFoundError, match="s3://bucket/missing"):
        s3.rename("missing", "destination")


@pytest.mark.parametrize("method_name", ["copy", "move", "rename"])
def test_copy_move_and_rename_reject_the_same_key(method_name):
    s3 = S3OS("bucket", FakeClient())

    with pytest.raises(ValueError, match="must be different"):
        getattr(s3, method_name)("same.txt", "same.txt")


def test_delete_objects_content_md5_header():
    request = type("Request", (), {"headers": {}, "body": b"<Delete />"})()

    S3OS._add_delete_objects_content_md5(request)

    assert request.headers["Content-MD5"] == base64.b64encode(
        hashlib.md5(b"<Delete />").digest()
    ).decode("ascii")


def test_permission_check_is_read_only_by_default():
    client = FakeClient()
    client.objects["sample.txt"] = b"hello"

    result = check_bucket_permissions(client, "bucket")

    assert result == {"list": True, "read": True, "write": None, "delete": None}


def test_helper_functions_return_values_instead_of_printing():
    client = FakeClient()
    client.paginator = FakePaginator(
        [
            {"Contents": [{"Key": "first.txt"}]},
            {"Contents": [{"Key": "second.txt"}]},
        ]
    )

    assert list_all_buckets(client) == ["alpha", "beta"]
    assert list_objects_in_bucket("bucket", client) == [
        {"Key": "first.txt"},
        {"Key": "second.txt"},
    ]
    assert client.paginator.calls == [{"Bucket": "bucket"}]
