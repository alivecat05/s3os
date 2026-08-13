import os

import boto3
from botocore.client import Config

from s3os import S3OS, check_bucket_permissions


def make_client():
    """Build a boto3 client entirely from environment variables."""
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
        config=Config(
            s3={"addressing_style": os.environ.get("S3_ADDRESSING_STYLE", "path")},
            signature_version=os.environ.get("S3_SIGNATURE_VERSION", "s3v4"),
        ),
    )


if __name__ == "__main__":
    bucket = os.environ["S3_BUCKET"]
    s3 = S3OS(bucket, make_client())

    print(check_bucket_permissions(s3.client, bucket))
    print(s3.listdir("datasets"))

    if os.environ.get("S3OS_RUN_WRITE_EXAMPLE") == "1":
        with s3.open("notes/hello.txt", "w") as file:
            file.write("hello from s3os\n")

        with s3.open("notes/hello.txt", "r") as file:
            print(file.read())
