import boto3
import io
import posixpath
import uuid
from contextlib import contextmanager
from botocore.client import Config
from botocore.exceptions import ClientError
from s3os import S3OS, list_all_buckets, list_objects_in_bucket, download_file, check_bucket_permissions
# 您的凭证与端点信息
ACCESS_KEY = 'YOUR_ACCESS_KEY'
SECRET_KEY = 'YOUR_SECRET_KEY'
# 注意：endpoint_url 只需要填 host_base 即可
ENDPOINT_URL = "YOUR_ENDPOINT_URL"

# 1. 初始化 S3 Client
s3_client = boto3.client(
    's3',
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    endpoint_url=ENDPOINT_URL,
    # 关键配置：强制使用 path-style，以匹配您的 host_bucket 格式
    config=Config(
        s3={'addressing_style': 'path'},
        signature_version='s3v4' # 通常现代的 S3 兼容存储都需要 v4 签名
    )
)
# 运行测试
if __name__ == '__main__':
    # 测试连接并列出桶
    list_all_buckets(s3_client)
    bucket = "YOUR_BUCKET_NAME"
    s3os = S3OS(bucket,s3_client)

    # 只读权限检查
    print(check_bucket_permissions(s3_client,bucket))
    # 完整读写检查，会创建并删除临时对象
    print(check_bucket_permissions(s3_client,bucket, test_write=True))

    for sub_dir in s3os.listdir("data_sg"):               #list path
        print(f"Subdirectory: {sub_dir}")
        sub_dir_path = s3os.join("data_sg", sub_dir)      #join path
        zed_dir = s3os.join(sub_dir_path, "zed")
        homie_dir = s3os.join(sub_dir_path, "homie")
        if s3os.isdir(zed_dir):
            print(f"  Found 'zed' directory in {sub_dir_path}")
            for file in s3os.listdir(zed_dir):
                if file.endswith(".svo2"):
                    print(f"    Found .svo2 file: {file}")
                    s3os.remove(s3os.join(zed_dir, file))
            frames_path = s3os.join(zed_dir, "frames")
            unsync_frames_path = s3os.join(zed_dir, "unsync_frames")
            if s3os.isdir(frames_path):
                print(f"  Found 'frames' directory in {zed_dir}, removing it.")
                s3os.rmtree(frames_path)
            if s3os.isdir(unsync_frames_path):
                print(f"  Found 'frames' directory in {zed_dir}, removing it.")
                s3os.rmtree(unsync_frames_path)
            frames = s3os.join(homie_dir, "frames")
            if s3os.isdir(frames):
                print(f"  Found 'cam_frames' directory in {homie_dir}, removing it.")
                s3os.rmtree(frames)
