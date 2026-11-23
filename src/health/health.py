"""GET /health: 앱이 데이터 리소스에 실제로 닿는지 한 번에 확인합니다.

- S3 문서 버킷: 최상위 폴더 목록 조회
- DynamoDB kb-registry: 연결된 KB 목록 조회
- Bedrock Titan 임베딩: 한 문장을 임베딩해서 벡터 길이(1024) 확인

공개 저장소와 보고서 화면에 계정 ID가 찍히지 않도록 응답에는 버킷 이름, ARN, 오류 원문을 넣지 않습니다.
오류 원문은 CloudWatch 로그에만 남깁니다.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "")
REGISTRY_TABLE = os.environ.get("REGISTRY_TABLE", "")
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))
APP_VERSION = os.environ.get("APP_VERSION", "local")

# API Gateway가 30초에서 끊으므로 각 호출은 짧게 기다리고 한 번만 다시 시도합니다.
_BOTO_CONFIG = Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 2, "mode": "standard"})
_clients = {}


class HealthCheckError(Exception):
    """응답에 그대로 보여도 되는 실패 사유 (직접 만든 문구만 담습니다)."""


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name, config=_BOTO_CONFIG)
    return _clients[name]


def check_s3():
    resp = _client("s3").list_objects_v2(Bucket=DOCS_BUCKET, Delimiter="/", MaxKeys=50)
    prefixes = sorted(p["Prefix"] for p in resp.get("CommonPrefixes", []))
    return {"prefixes": prefixes}


def check_registry():
    ddb = _client("dynamodb")
    kwargs = {"TableName": REGISTRY_TABLE, "ProjectionExpression": "kb_key, enabled"}
    items = []
    while True:
        resp = ddb.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    kb_keys = sorted(i["kb_key"]["S"] for i in items)
    enabled = sum(1 for i in items if i.get("enabled", {}).get("BOOL", False))
    return {"kb_keys": kb_keys, "enabled": enabled}


def check_embedding():
    body = json.dumps({"inputText": "health check", "dimensions": EMBED_DIM, "normalize": True})
    resp = _client("bedrock-runtime").invoke_model(
        modelId=EMBED_MODEL_ID, body=body, contentType="application/json", accept="application/json"
    )
    dim = len(json.loads(resp["body"].read())["embedding"])
    if dim != EMBED_DIM:
        raise HealthCheckError(f"vector dimension {dim}, expected {EMBED_DIM}")
    return {"model": EMBED_MODEL_ID, "dimension": dim}


def _run(name, fn):
    start = time.perf_counter()
    try:
        result = {"ok": True, **fn()}
    except ClientError as e:
        LOG.exception("check %s failed", name)
        result = {"ok": False, "error": e.response.get("Error", {}).get("Code", "ClientError")}
    except HealthCheckError as e:
        LOG.error("check %s failed: %s", name, e)
        result = {"ok": False, "error": str(e)}
    except Exception as e:  # 예상 못 한 오류도 503으로 돌려주고, 원문은 로그에만 남깁니다.
        LOG.exception("check %s failed", name)
        result = {"ok": False, "error": type(e).__name__}
    result["ms"] = round((time.perf_counter() - start) * 1000)
    return result


CHECKS = {
    "s3_docs": check_s3,
    "dynamodb_kb_registry": check_registry,
    "bedrock_embedding": check_embedding,
}


def handler(event, context):
    checks = {name: _run(name, fn) for name, fn in CHECKS.items()}
    healthy = all(c["ok"] for c in checks.values())
    body = {
        "status": "ok" if healthy else "degraded",
        "service": "club-rag-hub",
        "version": APP_VERSION,
        "region": os.environ.get("AWS_REGION", ""),
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": checks,
    }
    LOG.info(json.dumps({"health": body["status"], "checks": {k: v["ok"] for k, v in checks.items()}}))
    return {
        "statusCode": 200 if healthy else 503,
        "headers": {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"},
        "body": json.dumps(body, ensure_ascii=False),
    }
