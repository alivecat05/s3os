import boto3
import io
import posixpath
import uuid
from contextlib import contextmanager
from botocore.client import Config
from botocore.exceptions import ClientError



def list_all_buckets(s3_client):
    """列出当前账号下的所有 Bucket"""
    try:
        response = s3_client.list_buckets()
        print("您拥有的 Buckets:")
        for bucket in response.get('Buckets', []):
            print(f" - {bucket['Name']}")
    except Exception as e:
        print(f"获取 Buckets 失败: {e}")

def list_objects_in_bucket(bucket_name,s3_client):
    """列出指定 Bucket 中的文件（对象）"""
    try:
        response = s3_client.list_objects_v2(Bucket=bucket_name)
        print(f"\nBucket '{bucket_name}' 中的文件:")
        if 'Contents' in response:
            for obj in response['Contents']:
                print(f" - 文件名: {obj['Key']}, 大小: {obj['Size']} 字节")
        else:
            print("该 Bucket 为空。")
    except Exception as e:
        print(f"获取文件列表失败: {e}")

def download_file(s3_client,bucket_name, object_name, local_file_path):
    """下载文件"""
    try:
        s3_client.download_file(bucket_name, object_name, local_file_path)
        print(f"\n成功下载 {object_name} 到 {local_file_path}")
    except Exception as e:
        print(f"下载失败: {e}")


def check_bucket_permissions(s3_client,bucket_name, sample_key=None, test_write=False):
    """分别检查 Bucket 的列举、读取、写入和删除权限。

    test_write=False 时只做只读检查；写权限无法在不写入对象的情况下可靠确认。
    开启写检查后会写入一个唯一的临时对象，然后立即删除。
    """
    result = {
        'list': False,
        'read': None,
        'write': None,
        'delete': None,
    }

    def error_message(exc):
        error = exc.response.get('Error', {})
        return f"{error.get('Code', 'Unknown')}: {error.get('Message', str(exc))}"

    try:
        response = s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
        result['list'] = True
        if sample_key is None and response.get('Contents'):
            sample_key = response['Contents'][0]['Key']
    except ClientError as exc:
        result['list'] = error_message(exc)

    if sample_key is not None:
        try:
            response = s3_client.get_object(Bucket=bucket_name, Key=sample_key)
            response['Body'].close()
            result['read'] = True
        except ClientError as exc:
            result['read'] = error_message(exc)

    if not test_write:
        return result

    probe_key = f".permission-check/{uuid.uuid4().hex}"
    probe_created = False
    try:
        s3_client.put_object(
            Bucket=bucket_name,
            Key=probe_key,
            Body=b'boto3 permission check',
        )
        probe_created = True
        result['write'] = True
    except ClientError as exc:
        result['write'] = error_message(exc)
    finally:
        if probe_created:
            try:
                s3_client.delete_object(Bucket=bucket_name, Key=probe_key)
                result['delete'] = True
            except ClientError as exc:
                result['delete'] = error_message(exc)
                result['undeleted_probe_key'] = probe_key

    return result


class S3OS:
    """针对单个 Bucket 的轻量 os 风格访问层。

    S3 是对象存储，目录只是 Key 的前缀；因此这里使用
    posix 风格的 "/" 路径，不使用本机 os.path 的分隔符。
    """

    def __init__(self, bucket_name, client=None):
        self.bucket_name = bucket_name
        self.client = client

    @staticmethod
    def _key(path):
        if path is None:
            raise TypeError('path 不能是 None')
        return str(path).replace('\\', '/').strip('/')

    def join(self, *parts):
        """类似 os.path.join，但使用 S3 的 "/" 分隔符。"""
        cleaned = [self._key(part) for part in parts if str(part).strip('/\\')]
        return posixpath.join(*cleaned) if cleaned else ''

    def isfile(self, path):
        key = self._key(path)
        if not key:
            return False
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code in {'404', 'NoSuchKey', 'NotFound'}:
                return False
            raise

    def isdir(self, path):
        key = self._key(path)
        prefix = f'{key}/' if key else ''
        response = self.client.list_objects_v2(
            Bucket=self.bucket_name,
            Prefix=prefix,
            MaxKeys=1,
        )
        return bool(response.get('KeyCount', 0))

    def exists(self, path):
        return self.isfile(path) or self.isdir(path)

    def listdir(self, path=''):
        """返回直接子文件/子目录名，行为接近 os.listdir。"""
        key = self._key(path)
        prefix = f'{key}/' if key else ''
        names = set()
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(
            Bucket=self.bucket_name,
            Prefix=prefix,
            Delimiter='/',
        ):
            for item in page.get('Contents', []):
                relative = item['Key'][len(prefix):]
                if relative:
                    names.add(relative)
            for item in page.get('CommonPrefixes', []):
                relative = item['Prefix'][len(prefix):].rstrip('/')
                if relative:
                    names.add(relative)
        return sorted(names)

    @contextmanager
    def open(self, path, mode='rb', encoding='utf-8'):
        """
        类似内置 open，支持 r/rb/w/wb。

        读写会缓存整个对象，适合配置、JSON、文本等中小文件。
        大文件请使用 download_file/upload_file，避免占用过多内存。
        """
        if mode not in {'r', 'rb', 'w', 'wb'}:
            raise ValueError("仅支持 'r'、'rb'、'w'、'wb' 模式")

        key = self._key(path)
        if not key:
            raise ValueError('不能将 Bucket 根目录当作文件打开')

        if mode.startswith('r'):
            response = self.client.get_object(Bucket=self.bucket_name, Key=key)
            try:
                data = response['Body'].read()
            finally:
                response['Body'].close()
            stream = io.BytesIO(data) if 'b' in mode else io.StringIO(data.decode(encoding))
            try:
                yield stream
            finally:
                stream.close()
            return

        stream = io.BytesIO() if 'b' in mode else io.StringIO()
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

    def remove(self, path):
        """类似 os.remove：删除一个对象，不递归删除前缀。"""
        key = self._key(path)
        if not key:
            raise ValueError('不能删除 Bucket 根目录')
        self.client.delete_object(Bucket=self.bucket_name, Key=key)

    unlink = remove

    def rmtree(self, path):
        """递归删除 S3 目录，返回已删除的对象数量。"""
        key = self._key(path)
        if not key:
            raise ValueError('不能递归删除 Bucket 根目录')

        # S3 没有真实目录；只删除 "path/" 前缀，
        # 避免把 frames_backup 这类同名开头的 Key 一起删除。
        prefix = f'{key}/'
        deleted_count = 0

        while True:
            response = self.client.list_objects_v2(
                Bucket=self.bucket_name,
                Prefix=prefix,
                MaxKeys=1000,
            )
            objects = [{'Key': item['Key']} for item in response.get('Contents', [])]
            if not objects:
                break

            delete_response = self.client.delete_objects(
                Bucket=self.bucket_name,
                Delete={'Objects': objects, 'Quiet': True},
            )
            errors = delete_response.get('Errors', [])
            if errors:
                details = '; '.join(
                    f"{item.get('Key')}: {item.get('Code')} {item.get('Message', '')}"
                    for item in errors
                )
                raise RuntimeError(f'部分 S3 对象删除失败: {details}')

            deleted_count += len(objects)

        return deleted_count

    def download(self, path, local_file_path):
        self.client.download_file(self.bucket_name, self._key(path), local_file_path)

    def upload(self, local_file_path, path):
        self.client.upload_file(local_file_path, self.bucket_name, self._key(path))
