"""검색 핵심: 레지스트리로 KB를 고르고, KB마다 Retrieve를 동시에 불러 KB별로 묶어 돌려줍니다.

웹 질의응답 Lambda(src/ask/)와 MCP 도구 Lambda(src/tools/)가 같이 씁니다. HTTP도 MCP도 모르는 Python + boto3입니다.
- 레지스트리(DynamoDB kb-registry)에서 enabled이고 allowed_groups에 '*' 또는 호출자 그룹이 있는 KB만 씁니다.
- KB끼리 점수순으로 합치지 않습니다. 11/23 점검에서 동아리 점수가 학교 조문보다 높게 나와,
  점수순으로 자르면 정답인 학교 규정이 빠졌습니다. 어느 쪽을 쓸지는 읽는 쪽(LLM)이 정합니다.
- 출처(제목, 개정 정보, 링크)는 문서 메타데이터(.metadata.json)로 만듭니다.
- 응답과 로그에 계정 ID, 토큰, 버킷 이름을 넣지 않습니다. 문서 ID는 버킷을 뺀 S3 키입니다.

설정 (환경 변수, 없으면 로컬 기본값)
  REGISTRY_TABLE   기본 <RESOURCE_PREFIX>-kb-registry
  DOCS_BUCKET      기본 <RESOURCE_PREFIX>-docs-<계정 ID> (계정은 STS로 조회, read_document에서만 씀)
  RESOURCE_PREFIX  기본 club-rag-hub (infra/config.env의 PREFIX)
  AWS_REGION       기본 ap-northeast-2

입력이 잘못되면 InvalidRequest(ValueError)를 냅니다. 문구는 사용자에게 그대로 보여도 됩니다.
그 밖의 예외(레지스트리 조회 실패 등)는 부르는 쪽이 일반 오류로 처리하고 원문은 로그에만 남깁니다.
"""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger("kb_search")
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DEFAULT_PER_KB = 4
MAX_PER_KB = 10            # LLM 대화창이 넘치지 않게 묶음당 최대 10개
MAX_QUERY_CHARS = 1_000
MAX_DOC_CHARS = 20_000     # read_document 최대 글자 수
REGISTRY_TTL_SEC = 60      # Lambda가 살아 있는 동안 레지스트리를 기억하는 시간
BLOCKED_PARTS = {"raw", "_sources"}  # 원본 보관 폴더는 KB 폴더 설정이 넓어도 읽지 않습니다.

# 질의응답은 HTTP API 30초 안에 답까지 써야 하므로 각 호출은 짧게 기다리고 한 번만 다시 시도합니다.
_BOTO_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2, "mode": "standard"})
_clients = {}
_clients_lock = threading.Lock()
_cache = {"registry": None, "registry_at": 0.0, "bucket": None}
_DESER = TypeDeserializer()


class InvalidRequest(ValueError):
    """사용자에게 그대로 보여도 되는 입력 오류 (직접 만든 문구만 담습니다)."""


# ---------------------------------------------------------------- 설정과 클라이언트

def _region():
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-northeast-2"


def _prefix():
    return os.environ.get("RESOURCE_PREFIX") or "club-rag-hub"


def _client(name):
    # 스레드에서 처음 만들면 충돌할 수 있어서 잠금 안에서 한 번만 만듭니다. 만든 클라이언트는 스레드끼리 같이 써도 됩니다.
    with _clients_lock:
        if name not in _clients:
            _clients[name] = boto3.client(name, region_name=_region(), config=_BOTO_CONFIG)
        return _clients[name]


def registry_table():
    return os.environ.get("REGISTRY_TABLE") or f"{_prefix()}-kb-registry"


def docs_bucket():
    if os.environ.get("DOCS_BUCKET"):
        return os.environ["DOCS_BUCKET"]
    if not _cache["bucket"]:
        account = _client("sts").get_caller_identity()["Account"]
        _cache["bucket"] = f"{_prefix()}-docs-{account}"
    return _cache["bucket"]


def reset_cache():
    """테스트와 로컬 확인용: 기억해 둔 레지스트리, 버킷 이름, 클라이언트를 비웁니다."""
    _cache.update(registry=None, registry_at=0.0, bucket=None)
    with _clients_lock:
        _clients.clear()


def _redact(text):
    """오류 원문에 든 계정 ID(12자리 숫자)를 가립니다."""
    return re.sub(r"\d{12}", "************", str(text))


def _error_code(e):
    if isinstance(e, ClientError):
        return e.response.get("Error", {}).get("Code", "ClientError")
    return type(e).__name__


# ---------------------------------------------------------------- 레지스트리

def load_registry():
    """레지스트리 전체를 읽습니다. 값은 Python 형식(allowed_groups는 set)으로 바꿉니다."""
    now = time.monotonic()
    if _cache["registry"] is not None and now - _cache["registry_at"] < REGISTRY_TTL_SEC:
        return _cache["registry"]
    ddb = _client("dynamodb")
    kwargs = {"TableName": registry_table()}
    items = []
    while True:
        resp = ddb.scan(**kwargs)
        items.extend({k: _DESER.deserialize(v) for k, v in it.items()} for it in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    _cache.update(registry=items, registry_at=now)
    return items


def _clean_groups(caller_groups):
    if not isinstance(caller_groups, (list, tuple, set, frozenset)):
        return set()
    return {g for g in caller_groups if isinstance(g, str) and g and g != "*"}


def allowed_kbs(caller_groups):
    """호출자가 볼 수 있는 KB. 모두에게 열린 KB(학교 규정)를 먼저, 그다음 kb_key 순서입니다."""
    groups = _clean_groups(caller_groups)
    out = []
    for kb in load_registry():
        allowed = set(kb.get("allowed_groups") or ())
        if kb.get("enabled") is True and kb.get("kb_id") and kb.get("kb_key") and ("*" in allowed or allowed & groups):
            out.append(kb)
    return sorted(out, key=lambda kb: ("*" not in set(kb.get("allowed_groups") or ()), kb["kb_key"]))


def _kb_name(kb):
    return str(kb.get("name") or kb["kb_key"])


def list_sources(caller_groups):
    """검색할 수 있는 KB 목록: [{"kb_key", "name", "description"}]. 허용된 KB만 보여 줍니다."""
    return [
        {"kb_key": kb["kb_key"], "name": _kb_name(kb), "description": str(kb.get("description") or "")}
        for kb in allowed_kbs(caller_groups)
    ]


# ---------------------------------------------------------------- 메타데이터 → 출처

def _s(meta, key):
    value = meta.get(key)
    return "" if value is None else str(value).strip()


def describe(meta, doc_id=""):
    """메타데이터로 (kind, title, detail, url)을 만듭니다.

    학교: 「충남대학교 학칙」 제89조(출석인정) / 제2298호, 2026-09-29 개정
    동아리: 전체 일정 > 일정 및 작업들 > 여름 2차 회의 / 2026
    """
    kind = _s(meta, "doc_type")
    if not kind:
        kind = "school_rule" if meta.get("rule_name") else "club_manual" if meta.get("path") else "document"
    title = detail = ""
    if kind == "school_rule":
        rule, no, sub = _s(meta, "rule_name"), _s(meta, "article_no"), _s(meta, "article_title")
        if no.startswith("별표"):
            # 별표는 원문 표기처럼 「규정」 [별표 1] 제목. 제목 앞의 '별표'는 수집 때 붙은 것이라 뗍니다.
            sub = sub[2:].strip() if sub.startswith("별표") else sub
            title = " ".join(p for p in (f"「{rule}」" if rule else "", f"[{no}]", sub) if p)
        else:
            title = " ".join(p for p in (f"「{rule}」" if rule else "", f"{no}({sub})" if no and sub else no) if p)
        rev_date = _s(meta, "rev_date")
        detail = ", ".join(p for p in (_s(meta, "rev_no"), f"{rev_date} 개정" if rev_date else "") if p)
    elif kind == "club_manual":
        title = _s(meta, "path") or _s(meta, "title")
        detail = _s(meta, "year")
    if not title:
        title = _s(meta, "title") or doc_id.rsplit("/", 1)[-1]
    return kind, title, detail, _s(meta, "source_url")


def _key_from_uri(uri):
    """s3://버킷/school/kb/x.md → school/kb/x.md (버킷 이름에 계정 ID가 있어서 뗍니다)."""
    if not isinstance(uri, str) or not uri.startswith("s3://"):
        return ""
    parts = uri[len("s3://"):].split("/", 1)
    return parts[1] if len(parts) == 2 else ""


def to_doc(result):
    """Retrieve 결과 하나를 Doc으로 바꿉니다."""
    meta = result.get("metadata") or {}
    location = result.get("location") or {}
    doc_id = _key_from_uri((location.get("s3Location") or {}).get("uri", ""))
    kind, title, detail, url = describe(meta, doc_id)
    return {
        "doc_id": doc_id,
        "kind": kind,
        "title": title,
        "detail": detail,
        "url": url,
        "text": (result.get("content") or {}).get("text", ""),
        "score": round(float(result.get("score") or 0.0), 4),
    }


# ---------------------------------------------------------------- 검색

def _clean_query(query):
    if not isinstance(query, str) or not query.strip():
        raise InvalidRequest("검색할 질문을 적어 주세요.")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise InvalidRequest(f"질문을 {MAX_QUERY_CHARS:,}자 안으로 적어 주세요.")
    return query


def _clean_per_kb(per_kb):
    try:
        n = int(per_kb)
    except (TypeError, ValueError):
        n = DEFAULT_PER_KB
    return max(1, min(MAX_PER_KB, n))


def _clean_sources(sources):
    """None·빈 목록이면 None(전부). 문자열 하나도 받습니다. 순서를 지키고 겹치는 것은 뺍니다."""
    if sources is None:
        return None
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, (list, tuple)):
        raise InvalidRequest("sources는 KB 이름(kb_key) 목록으로 적어 주세요.")
    out = []
    for s in sources:
        if isinstance(s, str) and s.strip() and s.strip() not in out:
            out.append(s.strip())
    return out or None


def _retrieve(client, kb, query, per_kb):
    group = {"kb_key": kb["kb_key"], "name": _kb_name(kb), "results": []}
    try:
        resp = client.retrieve(
            knowledgeBaseId=kb["kb_id"],
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": per_kb}},
        )
        group["results"] = [to_doc(r) for r in resp.get("retrievalResults", [])][:per_kb]
    except (ClientError, BotoCoreError) as e:
        # KB 하나가 실패해도 나머지 결과는 돌려줍니다. 응답에는 오류 코드만, 로그에는 계정 ID를 가린 원문을 남깁니다.
        group["error"] = _error_code(e)
        LOG.error(json.dumps({"kb_search": "retrieve_failed", "kb_key": kb["kb_key"], "error": group["error"],
                              "message": _redact(e)}, ensure_ascii=False))
    except Exception as e:  # 예상 못 한 오류(결과 형식 등)도 그 KB만 실패로 둡니다.
        group["error"] = type(e).__name__
        LOG.exception("kb_search retrieve failed: %s", kb["kb_key"])
    return group


def search(query, caller_groups, sources=None, per_kb=DEFAULT_PER_KB):
    """허용된 KB마다 동시에 검색해서 KB별로 묶어 돌려줍니다.

    {"query": "...", "groups": [{"kb_key", "name", "results": [Doc, ...]}, ...], "skipped": ["club-ana"]}
    - sources를 주면 그 KB만 검색합니다. 권한이 없거나 없는 KB는 검색하지 않고 skipped에 넣습니다.
      (둘을 구분하지 않아야 권한 없는 사람에게 KB가 있는지조차 알려 주지 않습니다.)
    - 검색에 실패한 KB는 results가 빈 목록이고 error(오류 코드)가 붙습니다.
    """
    start = time.perf_counter()
    query = _clean_query(query)
    per_kb = _clean_per_kb(per_kb)
    wanted = _clean_sources(sources)
    kbs = allowed_kbs(caller_groups)
    skipped = []
    if wanted is not None:
        allowed_keys = {kb["kb_key"] for kb in kbs}
        skipped = [k for k in wanted if k not in allowed_keys]
        kbs = [kb for kb in kbs if kb["kb_key"] in wanted]

    groups = []
    if kbs:
        client = _client("bedrock-agent-runtime")
        with ThreadPoolExecutor(max_workers=len(kbs)) as pool:
            futures = [pool.submit(_retrieve, client, kb, query, per_kb) for kb in kbs]
            groups = [f.result() for f in futures]

    LOG.info(json.dumps({
        "kb_search": "search",
        "groups": sorted(_clean_groups(caller_groups)),
        "results": {g["kb_key"]: len(g["results"]) for g in groups},
        "failed": [g["kb_key"] for g in groups if "error" in g],
        "skipped": skipped,
        "ms": round((time.perf_counter() - start) * 1000),
    }, ensure_ascii=False))
    return {"query": query, "groups": groups, "skipped": skipped}


# ---------------------------------------------------------------- 문서 읽기

def _clean_doc_id(doc_id):
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise InvalidRequest("읽을 문서의 doc_id를 적어 주세요 (search 결과에 있습니다).")
    key = doc_id.strip()
    parts = key.split("/")
    if (
        len(key) > 512
        or "\\" in key
        or any(ord(c) < 32 for c in key)
        or any(p in ("", ".", "..") for p in parts)
        or BLOCKED_PARTS & set(parts)
        or key.endswith(".metadata.json")
    ):
        raise InvalidRequest("읽을 수 없는 문서 ID입니다. search 결과의 doc_id를 그대로 넣어 주세요.")
    return key


def _title_from_text(text):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _total_size(resp, fetched):
    """Range로 앞부분만 받았을 때 파일 전체 크기 (ContentRange: bytes 0-79999/123456)."""
    m = re.search(r"/(\d+)$", str(resp.get("ContentRange", "")))
    return int(m.group(1)) if m else int(resp.get("ContentLength", fetched))


def read_document(doc_id, caller_groups):
    """허용된 KB 폴더 안의 문서 전체를 읽습니다 (최대 2만 자).

    {"doc_id", "kind", "title", "detail", "url", "text", "truncated"}
    """
    key = _clean_doc_id(doc_id)
    kb = next(
        (kb for kb in allowed_kbs(caller_groups)
         if str(kb.get("s3_prefix", "")).endswith("/") and key.startswith(kb["s3_prefix"])),
        None,
    )
    if kb is None:
        raise InvalidRequest("읽을 수 없는 문서 ID입니다. search 결과의 doc_id를 그대로 넣어 주세요.")

    s3, bucket = _client("s3"), docs_bucket()
    try:
        # 한글은 UTF-8로 3바이트라 넉넉히 4배까지만 받습니다. 큰 파일을 통째로 받지 않으려는 것입니다.
        resp = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{MAX_DOC_CHARS * 4 - 1}")
        body = resp["Body"].read()
    except ClientError as e:
        code = _error_code(e)
        LOG.warning(json.dumps({"kb_search": "read_failed", "kb_key": kb["kb_key"], "doc_id": key, "error": code},
                               ensure_ascii=False))
        # 함수에 ListBucket 권한이 없으면 없는 파일도 AccessDenied로 옵니다.
        if code in ("NoSuchKey", "404", "AccessDenied", "403"):
            raise InvalidRequest(f"문서를 찾지 못했습니다: {key}") from None
        if code != "InvalidRange":  # 빈 파일에 Range를 주면 InvalidRange가 옵니다.
            raise
        resp, body = {}, b""

    text = body.decode("utf-8", errors="ignore")  # 잘린 끝 글자는 버립니다.
    truncated = len(text) > MAX_DOC_CHARS or _total_size(resp, len(body)) > len(body)
    text = text[:MAX_DOC_CHARS]

    meta = {}
    try:
        raw = s3.get_object(Bucket=bucket, Key=f"{key}.metadata.json")["Body"].read()
        meta = json.loads(raw).get("metadataAttributes") or {}
    except (ClientError, ValueError, AttributeError):
        pass  # 메타데이터가 없으면 본문 제목을 씁니다.
    kind, title, detail, url = describe(meta, key)
    if not meta:
        title = _title_from_text(text) or title

    LOG.info(json.dumps({"kb_search": "read", "kb_key": kb["kb_key"], "doc_id": key, "chars": len(text),
                         "truncated": truncated}, ensure_ascii=False))
    return {"doc_id": key, "kind": kind, "title": title, "detail": detail, "url": url, "text": text,
            "truncated": truncated}
