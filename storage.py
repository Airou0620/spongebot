from __future__ import annotations

import os
import threading
from pathlib import PurePosixPath

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _env(primary: str, fallback: str | None = None, default: str | None = None) -> str:
    value = os.getenv(primary)
    if not value and fallback:
        value = os.getenv(fallback)
    if not value:
        value = default
    if value is None:
        raise RuntimeError(f"缺少環境變數：{primary}")
    return value


class MemeStorage:
    """
    Railway S3-compatible Bucket storage.

    預設 Bucket 結構：
      memes/
        *.jpg
        YN/*.jpg

      airou/
        *.jpg
      或舊資料夾名稱：
      Airou/
        *.jpg

      PhotoReceived/
        <user><num>.jpg

      logs/
        searchTG_output.log
        searchDC_output.log
    """

    def __init__(self):
        self.endpoint = _env("ENDPOINT", "AWS_ENDPOINT_URL")
        self.access_key = _env("ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
        self.secret_key = _env("SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
        self.region = (
            os.getenv("REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "auto"
        )
        self.bucket = _env("BUCKET", "AWS_S3_BUCKET_NAME")

        self.meme_prefix = os.getenv("MEME_PREFIX", "memes").strip("/")
        self.airou_prefix = os.getenv("AIROU_PREFIX", "airou").strip("/")
        self.received_prefix = os.getenv("RECEIVED_PREFIX", "PhotoReceived").strip("/")
        self.log_prefix = os.getenv("LOG_PREFIX", "logs").strip("/")

        self.s3 = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

        self._log_lock = threading.Lock()

    @staticmethod
    def _is_image(key: str) -> bool:
        return PurePosixPath(key).suffix.lower() in IMAGE_EXTENSIONS

    @staticmethod
    def _join(prefix: str, name: str = "") -> str:
        if prefix and name:
            return f"{prefix}/{name}"
        return prefix or name

    def _list_keys(self, prefix: str) -> list[str]:
        prefix = prefix.strip("/")
        query = f"{prefix}/" if prefix else ""

        result: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")

        for page in paginator.paginate(Bucket=self.bucket, Prefix=query):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if self._is_image(key):
                    result.append(key)

        return result

    def list_memes(self) -> list[str]:
        """等價於原本 os.listdir(dir_path) 後取頂層圖片。"""
        root = f"{self.meme_prefix}/"
        result = []

        for key in self._list_keys(self.meme_prefix):
            rel = key[len(root):] if key.startswith(root) else key
            if rel and "/" not in rel:
                result.append(rel)

        return sorted(result)

    def list_yn(self) -> list[str]:
        """等價於原本 os.listdir(dir_path + "/YN")。"""
        prefix = self._join(self.meme_prefix, "YN")
        root = f"{prefix}/"
        result = []

        for key in self._list_keys(prefix):
            rel = key[len(root):] if key.startswith(root) else key
            if rel and "/" not in rel:
                result.append(rel)

        return sorted(result)

    def list_airou(self) -> list[str]:
        """
        等價於原本 os.listdir(.../Airou)。
        先用目前 Railway 上傳慣例 airou/；若空，再相容 Airou/。
        """
        prefixes = [self.airou_prefix]
        if self.airou_prefix != "Airou":
            prefixes.append("Airou")

        for prefix in prefixes:
            root = f"{prefix}/"
            result = []
            for key in self._list_keys(prefix):
                rel = key[len(root):] if key.startswith(root) else key
                if rel and "/" not in rel:
                    result.append(rel)
            if result:
                return sorted(result)

        return []

    def _get_bytes(self, key: str) -> bytes:
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def get_meme(self, filename: str) -> bytes:
        return self._get_bytes(self._join(self.meme_prefix, filename))

    def get_yn(self, filename: str) -> bytes:
        return self._get_bytes(
            self._join(self.meme_prefix, f"YN/{filename}")
        )

    def get_airou(self, filename: str) -> bytes:
        # 與 list_airou() 同樣相容 airou/ 與 Airou/
        candidates = [self.airou_prefix]
        if self.airou_prefix != "Airou":
            candidates.append("Airou")

        last_error = None
        for prefix in candidates:
            try:
                return self._get_bytes(self._join(prefix, filename))
            except ClientError as e:
                last_error = e
                code = e.response.get("Error", {}).get("Code", "")
                if code not in ("NoSuchKey", "404"):
                    raise

        if last_error:
            raise last_error
        raise FileNotFoundError(filename)

    def save_received(self, filename: str, data: bytes) -> str:
        """
        原本：
          PhotoReceived\\{user_full_name}{num}.jpg

        Railway：
          Bucket/PhotoReceived/{user_full_name}{num}.jpg
        """
        key = self._join(self.received_prefix, filename)
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="image/jpeg",
        )
        return key

    def append_text_log(self, filename: str, line: str) -> str:
        """
        S3 本身沒有 append，所以用 get -> append -> put，
        讓 Bucket 裡仍保持與原程式一樣的一個 .log 檔。

        Bucket/logs/searchTG_output.log
        Bucket/logs/searchDC_output.log
        """
        key = self._join(self.log_prefix, filename)
        new_line = str(line) + "\n"

        with self._log_lock:
            old = b""

            try:
                obj = self.s3.get_object(Bucket=self.bucket, Key=key)
                old = obj["Body"].read()
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code not in ("NoSuchKey", "404"):
                    raise

            data = old + new_line.encode("utf-8")

            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType="text/plain; charset=utf-8",
            )

            print(
            f"[BucketLog] WRITE OK bucket={self.bucket} key={key} bytes={len(data)}",
            flush=True
            )
            
        return key
