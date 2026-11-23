"""/health 핸들러 단위 테스트. AWS를 부르지 않고 가짜 클라이언트로 확인합니다."""

import io
import json

import pytest
from botocore.exceptions import ClientError

import health


class FakeS3:
    def list_objects_v2(self, **kw):
        assert kw["Bucket"] == "docs-bucket"
        return {"CommonPrefixes": [{"Prefix": "school/"}, {"Prefix": "clubs/"}]}


class FakeDynamo:
    def __init__(self):
        self.pages = [
            {"Items": [{"kb_key": {"S": "school"}, "enabled": {"BOOL": True}}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"kb_key": {"S": "club-ana"}, "enabled": {"BOOL": False}}]},
        ]

    def scan(self, **kw):
        assert kw["TableName"] == "registry-table"
        return self.pages.pop(0)


class FakeBedrock:
    def __init__(self, dim=1024, error=None):
        self.dim = dim
        self.error = error

    def invoke_model(self, **kw):
        if self.error:
            raise self.error
        assert json.loads(kw["body"])["dimensions"] == 1024
        return {"body": io.BytesIO(json.dumps({"embedding": [0.1] * self.dim}).encode())}


@pytest.fixture
def clients(monkeypatch):
    fakes = {"s3": FakeS3(), "dynamodb": FakeDynamo(), "bedrock-runtime": FakeBedrock()}
    monkeypatch.setattr(health, "_client", lambda name: fakes[name])
    monkeypatch.setattr(health, "DOCS_BUCKET", "docs-bucket")
    monkeypatch.setattr(health, "REGISTRY_TABLE", "registry-table")
    return fakes


def call():
    resp = health.handler({}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_all_checks_ok(clients):
    status, body = call()
    assert status == 200
    assert body["status"] == "ok"
    assert body["checks"]["s3_docs"]["prefixes"] == ["clubs/", "school/"]
    assert body["checks"]["dynamodb_kb_registry"]["kb_keys"] == ["club-ana", "school"]
    assert body["checks"]["dynamodb_kb_registry"]["enabled"] == 1
    assert body["checks"]["bedrock_embedding"]["dimension"] == 1024


def test_aws_error_shows_code_only(clients):
    # 오류 원문에는 계정 ID가 든 ARN이 들어갈 수 있으므로 응답에는 오류 코드만 나가야 합니다.
    msg = "User arn:aws:iam::123456789012:role/x is not authorized"
    clients["bedrock-runtime"].error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": msg}}, "InvokeModel"
    )
    status, body = call()
    assert status == 503
    assert body["status"] == "degraded"
    assert body["checks"]["bedrock_embedding"]["ok"] is False
    assert body["checks"]["bedrock_embedding"]["error"] == "AccessDeniedException"
    assert body["checks"]["s3_docs"]["ok"] is True
    assert "123456789012" not in json.dumps(body)


def test_wrong_dimension_fails(clients):
    clients["bedrock-runtime"].dim = 512
    status, body = call()
    assert status == 503
    assert body["checks"]["bedrock_embedding"]["error"] == "vector dimension 512, expected 1024"
