"""원문 보관 — Cloudflare R2 (S3 호환).

원문은 절대 git 에 들어가지 않고, DB 에도 들어가지 않는다.
하루 1개 gzip JSONL 파일로 묶는다 — 기사 1건 = 객체 1개면 요청 수가 폭발한다.

R2 자격증명이 없으면 로컬 디렉토리에 쓴다 (dry-run·개발용).
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path
from typing import Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_DIR = REPO_ROOT / "data" / "raw"


def blob_key(date: str) -> str:
    """하루치 원문이 들어가는 객체 키."""
    return f"raw/{date[:4]}/{date}.jsonl.gz"


class BlobStore:
    """R2 또는 로컬 파일. 인터페이스는 같다."""

    def __init__(self, *, local_only: bool = False, local_dir: Path | None = None):
        self.local_dir = Path(local_dir or LOCAL_DIR)
        self.bucket = os.environ.get("R2_BUCKET", "news-lens")
        self._s3 = None
        self.local_only = local_only or not self._has_credentials()
        if self.local_only:
            self.local_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _has_credentials() -> bool:
        return all(
            os.environ.get(k)
            for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
        )

    def _client(self):
        if self._s3 is None:
            import boto3  # noqa: PLC0415

            account = os.environ["R2_ACCOUNT_ID"]
            self._s3 = boto3.client(
                "s3",
                endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
            )
        return self._s3

    def _local_path(self, key: str) -> Path:
        return self.local_dir / key.replace("/", os.sep)

    # ── 쓰기 ──────────────────────────────────────────────────────────

    def append_day(self, date: str, rows: Iterable[dict]) -> tuple[str, int]:
        """하루치에 줄을 덧붙인다. 돌려주는 것은 (키, 시작 줄번호).

        R2 는 append 가 없으므로 기존 객체를 읽어 합쳐 다시 올린다.
        하루 1,000건 수준에서는 이게 가장 단순하고 싸다.
        """
        key = blob_key(date)
        existing = list(self.read_day(date))
        start = len(existing)
        payload = existing + list(rows)
        if start == len(payload):
            return key, start

        blob = gzip.compress(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in payload).encode("utf-8")
        )
        if self.local_only:
            path = self._local_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        else:
            self._client().put_object(Bucket=self.bucket, Key=key, Body=blob)
        return key, start

    # ── 읽기 ──────────────────────────────────────────────────────────

    def read_day(self, date: str) -> Iterator[dict]:
        key = blob_key(date)
        try:
            if self.local_only:
                path = self._local_path(key)
                if not path.exists():
                    return iter(())
                blob = path.read_bytes()
            else:
                blob = self._client().get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001 — 없으면 빈 것으로 본다
            if "NoSuchKey" not in str(exc) and "404" not in str(exc):
                print(f"[blobs] {key} 읽기 실패: {exc}", file=sys.stderr)
            return iter(())

        text = gzip.decompress(blob).decode("utf-8")
        return iter(
            [json.loads(line) for line in text.splitlines() if line.strip()]
        )


def store(**kwargs) -> BlobStore:
    return BlobStore(**kwargs)
