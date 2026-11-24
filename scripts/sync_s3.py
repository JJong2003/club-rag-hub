#!/usr/bin/env python3
"""로컬 data/ 폴더를 S3 문서 버킷에 올리고, 내용이 바뀐 KB는 동기화(ingestion)까지 한다.

data/ 아래 경로가 그대로 S3 키가 된다.
    data/school/kb/hakchik__a001.md  →  s3://<문서 버킷>/school/kb/hakchik__a001.md

사용법 (저장소 루트):
    python scripts/sync_s3.py school           # 학교 규정 (원본 + 조문) → KB school 동기화
    python scripts/sync_s3.py clubs/ana        # ANA 자료 → KB club-ana 동기화 (가림 검토 후)
    python scripts/sync_s3.py _sources         # 수집 원본 보관 (KB 없음)
    python scripts/sync_s3.py school --dry-run # 무엇을 올리고 지울지만 보기
    옵션: --dry-run, --no-ingest(올리기만), --ingest(바뀐 게 없어도 KB 동기화)

규칙:
    - 올리기: 로컬에 있는데 S3에 없거나 내용(MD5)이 다른 파일만 올린다.
    - 지우기: KB가 읽는 접두어(kb-registry의 s3_prefix) 안에서만, 로컬에 없는 S3 파일을 지운다.
      다시 만든 조문 목록과 KB가 어긋나지 않게 하려는 것. raw/, _sources/ 등은 지우지 않고 쌓기만 한다.
      로컬에 그 KB 폴더가 없거나 비어 있으면 지우지 않는다 (다른 PC에서 실수로 KB를 비우는 것 방지).
      문서 버킷은 버전 관리가 켜져 있어서 지운 파일도 이전 버전으로 되돌릴 수 있다.
    - KB 동기화: 그 KB 접두어에 올리거나 지운 파일이 있으면 ingestion 작업을 시작하고 끝날 때까지 기다린다.

AWS 프로필과 리전은 환경 변수(AWS_PROFILE, AWS_REGION)를 먼저 쓰고, 없으면 infra/config.env 값을 쓴다.
"""
from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO / "data"
CONFIG_ENV = REPO / "infra" / "config.env"

SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
SKIP_DIRS = {"__pycache__", "debug"}  # debug/: crawl_cnu_plus.py가 구조 확인용으로 남기는 화면
CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".hwp": "application/x-hwp",
    ".hwpx": "application/hwp+zip",
    ".gz": "application/gzip",
}
INGEST_TIMEOUT_SEC = 30 * 60


# ---------------------------------------------------------------- 계획 (AWS 없이 테스트하는 부분)

def to_prefix(target: str) -> str:
    """'data/school', 'school/', 'data\\clubs\\ana' → 'school/', 'clubs/ana/'"""
    t = target.replace("\\", "/").strip().strip("/")
    if t == "data":
        raise ValueError("data/ 전체는 받지 않습니다. 하위 폴더를 지정하세요 (school, clubs/ana, _sources)")
    if t.startswith("data/"):
        t = t[len("data/"):]
    if not t or t.startswith(".") or ".." in t.split("/"):
        raise ValueError(f"올바르지 않은 경로: {target}")
    return t + "/"


def targets_for(paths: list[Path | str]) -> list[str]:
    """다른 스크립트가 만든 폴더를 data/ 기준 경로로 바꾼다. data/ 밖의 경로는 알리고 뺀다."""
    out = []
    for p in paths:
        try:
            rel = Path(p).resolve().relative_to(DATA_ROOT)
        except ValueError:
            print(f"data/ 밖이라 S3에 올리지 않음: {p}")
            continue
        if rel.parts and rel.as_posix() not in out:
            out.append(rel.as_posix())
    return out


def kb_scope(prefix: str, kb_prefix: str) -> str | None:
    """올리는 접두어와 KB 접두어가 겹치면, 지우기를 적용할 범위(둘 중 더 좁은 쪽)를 돌려준다."""
    if kb_prefix.startswith(prefix):
        return kb_prefix
    if prefix.startswith(kb_prefix):
        return prefix
    return None


def plan_sync(local: dict[str, tuple[str, int]], remote: dict[str, tuple[str, int]],
              mirror_scopes: list[str]) -> tuple[list[str], list[str], list[str]]:
    """올릴 키, 지울 키, 로컬이 비어서 지우기를 막은 범위를 돌려준다.

    local, remote: {S3 키: (MD5 또는 ETag, 크기)}
    """
    uploads = []
    for key, (md5, size) in local.items():
        r = remote.get(key)
        if r is None:
            uploads.append(key)
            continue
        etag, rsize = r
        if "-" in etag:  # 멀티파트 ETag는 MD5가 아니라서 크기만 비교한다
            if rsize != size:
                uploads.append(key)
        elif etag != md5:
            uploads.append(key)

    deletes, blocked = [], []
    for scope in mirror_scopes:
        if not any(k.startswith(scope) for k in local):
            blocked.append(scope)
            continue
        deletes += [k for k in remote if k.startswith(scope) and k not in local]
    return sorted(uploads), sorted(set(deletes)), blocked


def scan_local(prefix: str) -> dict[str, Path]:
    base = DATA_ROOT / prefix.rstrip("/")
    files = {}
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name in SKIP_NAMES:
            continue
        rel = p.relative_to(DATA_ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        files[rel.as_posix()] = p
    return files


def md5_of(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_type(p: Path) -> str:
    return CONTENT_TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"


# ---------------------------------------------------------------- AWS

def read_config() -> dict[str, str]:
    cfg = {}
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^([A-Z_]+)="([^"$]*)"', line.strip())
            if m:
                cfg[m.group(1)] = m.group(2)
    return cfg


class Aws:
    def __init__(self):
        import boto3  # 계획만 테스트할 때는 boto3가 없어도 되게 여기서 부른다

        cfg = read_config()
        profile = os.environ.get("AWS_PROFILE") or cfg.get("AWS_PROFILE_NAME") or None
        region = os.environ.get("AWS_REGION") or cfg.get("AWS_REGION") or "ap-northeast-2"
        prefix = cfg.get("PREFIX", "club-rag-hub")
        session = boto3.Session(profile_name=profile, region_name=region)
        account = session.client("sts").get_caller_identity()["Account"]
        self.bucket = f"{prefix}-docs-{account}"
        self.registry_table = f"{prefix}-kb-registry"
        self.s3 = session.client("s3")
        self.ddb = session.client("dynamodb")
        self.agent = session.client("bedrock-agent")

    def registry(self) -> list[dict]:
        items, kw = [], {"TableName": self.registry_table}
        while True:
            page = self.ddb.scan(**kw)
            for it in page.get("Items", []):
                items.append({k: next(iter(v.values())) for k, v in it.items()})
            if "LastEvaluatedKey" not in page:
                return items
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def list_remote(self, prefix: str) -> dict[str, tuple[str, int]]:
        out = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                if o["Key"].endswith("/"):  # 콘솔에서 만든 빈 폴더 표시
                    continue
                out[o["Key"]] = (o["ETag"].strip('"'), o["Size"])
        return out

    def upload(self, key: str, path: Path) -> None:
        with path.open("rb") as f:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=f, ContentType=content_type(path))

    def delete(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):
            resp = self.s3.delete_objects(Bucket=self.bucket, Delete={
                "Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})
            if resp.get("Errors"):
                raise RuntimeError(f"S3 삭제 실패: {resp['Errors'][:3]}")

    def ingest(self, kb: dict) -> bool:
        from botocore.exceptions import ClientError

        kb_id, ds_id = kb["kb_id"], kb["data_source_id"]
        for _ in range(60):  # 이미 돌고 있는 작업이 있으면 끝날 때까지 기다렸다 다시 시작
            try:
                job = self.agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id,
                                                     description="scripts/sync_s3.py")["ingestionJob"]
                break
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConflictException":
                    raise
                print(f"  {kb['kb_key']}: 이미 동기화 중인 작업이 있어서 기다립니다...")
                time.sleep(10)
        else:
            print(f"  {kb['kb_key']}: 앞선 동기화가 끝나지 않아 시작하지 못했습니다")
            return False

        job_id, start = job["ingestionJobId"], time.time()
        print(f"  {kb['kb_key']}: KB 동기화 시작 (작업 {job_id})")
        while job["status"] in ("STARTING", "IN_PROGRESS", "STOPPING"):
            if time.time() - start > INGEST_TIMEOUT_SEC:
                print(f"  {kb['kb_key']}: {INGEST_TIMEOUT_SEC // 60}분이 지나도 끝나지 않음. 콘솔에서 확인하세요")
                return False
            time.sleep(5)
            job = self.agent.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id,
                                               ingestionJobId=job_id)["ingestionJob"]
        st = job.get("statistics", {})
        print(f"  {kb['kb_key']}: {job['status']} ({int(time.time() - start)}초) "
              f"스캔 {st.get('numberOfDocumentsScanned', 0)}, "
              f"새로 색인 {st.get('numberOfNewDocumentsIndexed', 0)}, "
              f"수정 {st.get('numberOfModifiedDocumentsIndexed', 0)}, "
              f"삭제 {st.get('numberOfDocumentsDeleted', 0)}, "
              f"실패 {st.get('numberOfDocumentsFailed', 0)}, "
              f"메타데이터 {st.get('numberOfMetadataDocumentsScanned', 0)}")
        for reason in job.get("failureReasons", [])[:10]:
            print("    실패 이유:", reason)
        return job["status"] == "COMPLETE" and st.get("numberOfDocumentsFailed", 0) == 0


# ---------------------------------------------------------------- 실행

def sync(targets: list[str], dry_run: bool = False, ingest: str = "auto") -> int:
    """targets: data/ 아래 경로들. ingest: auto(바뀐 KB만) | always | never. 실패가 있으면 1을 돌려준다."""
    prefixes = [to_prefix(t) for t in targets]
    for p in prefixes:
        if not (DATA_ROOT / p.rstrip("/")).is_dir():
            print(f"로컬 폴더가 없습니다: data/{p}")
            return 1

    aws = Aws()
    registry = aws.registry()
    print(f"문서 버킷: {aws.bucket}" + ("  (dry-run: 올리거나 지우지 않음)" if dry_run else ""))
    touched: dict[str, dict] = {}  # kb_key → 레지스트리 항목 (KB 동기화 대상)
    failed = False

    for prefix in prefixes:
        local_paths = scan_local(prefix)
        local = {k: (md5_of(p), p.stat().st_size) for k, p in local_paths.items()}
        remote = aws.list_remote(prefix)
        kbs = [(kb, kb_scope(prefix, kb["s3_prefix"])) for kb in registry]
        kbs = [(kb, scope) for kb, scope in kbs if scope]
        uploads, deletes, blocked = plan_sync(local, remote, [scope for _, scope in kbs])

        print(f"\n[{prefix}] 로컬 {len(local)}개, S3 {len(remote)}개 → 올리기 {len(uploads)}, 지우기 {len(deletes)}")
        for scope in blocked:
            print(f"  경고: 로컬 data/{scope}가 비어 있어서 S3 {scope} 파일은 지우지 않았습니다")
        for k in uploads[:5]:
            print("  +", k)
        for k in deletes[:5]:
            print("  -", k)
        if len(uploads) > 5 or len(deletes) > 5:
            print("  ...")

        if not dry_run:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda k: aws.upload(k, local_paths[k]), uploads))
            if deletes:
                aws.delete(deletes)

        for kb, scope in kbs:
            changed = any(k.startswith(scope) for k in uploads + deletes)
            if ingest == "always" or (ingest == "auto" and changed):
                touched[kb["kb_key"]] = kb
            elif ingest == "auto":
                print(f"  KB {kb['kb_key']}: {scope}에 바뀐 파일이 없어 동기화하지 않음")

    if dry_run:
        if touched:
            print("\n동기화할 KB:", ", ".join(touched))
        return 0
    if touched:
        print()
    for kb in touched.values():
        if not aws.ingest(kb):
            failed = True
    print("\n완료" if not failed else "\nKB 동기화에 실패한 항목이 있습니다")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", help="data/ 아래 폴더 (예: school, clubs/ana, _sources)")
    ap.add_argument("--dry-run", action="store_true", help="올리거나 지우지 않고 계획만 보여주기")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--no-ingest", action="store_true", help="S3에 올리기만 하고 KB 동기화는 하지 않기")
    g.add_argument("--ingest", action="store_true", help="바뀐 파일이 없어도 KB 동기화하기")
    args = ap.parse_args()
    try:
        return sync(args.targets, args.dry_run, "never" if args.no_ingest else "always" if args.ingest else "auto")
    except ValueError as e:
        print(e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
