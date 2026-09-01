"""
Railway Bucket 共用儲存層。
Discord / Telegram Bot 都用這一份。

Bucket 建議結構：
memes/
  【S1】....jpg
  【S2】....jpg
  YN/
    【SS1456】可以.jpg
airou/
  ...
received/
  ...
"""

from __future__ import annotations

import os
import time
import threading
from io import BytesIO
from pathlib import PurePosixPath

import boto3
from botocore.config import Config


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))


def _env(primary: str, aws_name: str | None = None) -> str:
    value = os.getenv(primary)
    if not value and aws_name:
        value = os.getenv(aws_name)
    if not value:
        raise RuntimeError(f"缺少環境變數：{primary}")
    return value


class MemeStorage:
    def __init__(self):
        self.endpoint = _env("ENDPOINT", "AWS_ENDPOINT_URL")
        self.access_key = _env("ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
        self.secret_key = _env("SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
        self.region = os.getenv("REGION") or os.getenv("AWS_DEFAULT_REGION") or "auto"
        self.bucket = _env("BUCKET", "AWS_S3_BUCKET_NAME")

        self.meme_prefix = os.getenv("MEME_PREFIX", "memes").strip("/")
        self.airou_prefix = os.getenv("AIROU_PREFIX", "airou").strip("/")
        self.received_prefix = os.getenv("RECEIVED_PREFIX", "received").strip("/")

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

        self._lock = threading.Lock()
        self._cache_time = 0.0
        self._memes: list[str] = []
        self._yn: list[str] = []
        self._airou: list[str] = []

    @staticmethod
    def _is_image(name: str) -> bool:
        return PurePosixPath(name).suffix.lower() in IMAGE_EXTENSIONS

    @staticmethod
    def _join(prefix: str, name: str = "") -> str:
        if prefix and name:
            return f"{prefix}/{name}"
        return prefix or name

    def _list_keys(self, prefix: str) -> list[str]:
        prefix_query = f"{prefix}/" if prefix else ""
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")

        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix_query):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if self._is_image(key):
                    keys.append(key)

        return keys

    def refresh(self) -> None:
        """重新讀 Bucket 物件清單；只拿檔名，不下載圖片。"""
        with self._lock:
            meme_root = f"{self.meme_prefix}/" if self.meme_prefix else ""
            all_meme_keys = self._list_keys(self.meme_prefix)

            memes: list[str] = []
            yn: list[str] = []

            for key in all_meme_keys:
                rel = key[len(meme_root):] if meme_root and key.startswith(meme_root) else key

                if rel.startswith("YN/"):
                    yn_name = rel[len("YN/"):]
                    if yn_name and "/" not in yn_name:
                        yn.append(yn_name)
                elif rel and "/" not in rel:
                    memes.append(rel)

            airou_root = f"{self.airou_prefix}/" if self.airou_prefix else ""
            airou = []
            for key in self._list_keys(self.airou_prefix):
                rel = key[len(airou_root):] if airou_root and key.startswith(airou_root) else key
                if rel and "/" not in rel:
                    airou.append(rel)

            self._memes = sorted(memes)
            self._yn = sorted(yn)
            self._airou = sorted(airou)
            self._cache_time = time.monotonic()

    def _ensure_cache(self) -> None:
        if time.monotonic() - self._cache_time > CACHE_TTL_SECONDS:
            self.refresh()

    def list_memes(self) -> list[str]:
        self._ensure_cache()
        return list(self._memes)

    def list_yn(self) -> list[str]:
        self._ensure_cache()
        return list(self._yn)

    def list_airou(self) -> list[str]:
        self._ensure_cache()
        return list(self._airou)

    def search_memes(self, keyword: str) -> list[str]:
        keyword = keyword.casefold()
        return [f for f in self.list_memes() if keyword in f.casefold()]

    def _get_bytes(self, key: str) -> bytes:
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def get_meme(self, filename: str) -> bytes:
        return self._get_bytes(self._join(self.meme_prefix, filename))

    def get_yn(self, filename: str) -> bytes:
        return self._get_bytes(self._join(self.meme_prefix, f"YN/{filename}"))

    def get_airou(self, filename: str) -> bytes:
        return self._get_bytes(self._join(self.airou_prefix, filename))

    def save_received(self, filename: str, data: bytes, content_type: str = "image/jpeg") -> None:
        key = self._join(self.received_prefix, filename)
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
