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
        self.paginator = FakePaginator([])
        self.list_pages = []

    def list_buckets(self):
        return {"Buckets": [{"Name": "alpha"}, {"Name": "beta"}]}

    def list_objects_v2(self, **kwargs):
        if self.list_pages:
            return self.list_pages.pop(0)
        prefix = kwargs.get("Prefix", "")
        contents = [{"Key": key} for key in self.objects if key.startswith(prefix)]
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
        for item in Delete["Objects"]:
            self.deleted.append(item["Key"])
            self.objects.pop(item["Key"], None)
        return {}

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
