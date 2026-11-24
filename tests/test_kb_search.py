"""검색 핵심(kb_search) 단위 테스트. AWS를 부르지 않고 가짜 클라이언트로 확인합니다."""

import io
import json
import threading

import pytest
from botocore.exceptions import ClientError

import kb_search
from kb_search import core

BUCKET = "club-rag-hub-docs-123456789012"


def kb_item(kb_key, kb_id, prefix, groups, enabled=True, name=None, description=None):
    item = {
        "kb_key": {"S": kb_key},
        "kb_id": {"S": kb_id},
        "s3_prefix": {"S": prefix},
        "allowed_groups": {"SS": groups},
        "enabled": {"BOOL": enabled},
    }
    if name:
        item["name"] = {"S": name}
    if description:
        item["description"] = {"S": description}
    return item


SCHOOL_META = {
    "doc_type": "school_rule",
    "rule_name": "충남대학교 전열기구 사용관리 규정",
    "rev_no": "제1042호",
    "rev_date": "2001-10-29",
    "article_no": "제4조",
    "article_title": "사용 승인범위",
    "source_url": "https://www.law.go.kr/학칙공단/충남대학교전열기구사용관리규정",
    "x-amz-bedrock-kb-chunk-id": "c1",
}
CLUB_META = {
    "doc_type": "club_manual",
    "title": "여름 2차 회의",
    "path": "전체 일정 > 일정 및 작업들 > 여름 2차 회의",
    "year": "2026",
    "source_url": "https://www.notion.so/abc",
}


def hit(key, meta, score, text="근거 문단"):
    return {
        "content": {"text": text, "type": "TEXT"},
        "location": {"type": "S3", "s3Location": {"uri": f"s3://{BUCKET}/{key}"}},
        "metadata": meta,
        "score": score,
    }


class FakeDynamo:
    def __init__(self):
        self.scans = 0
        self.pages = [
            {"Items": [
                kb_item("school", "KB_SCHOOL", "school/kb/", ["*"], name="충남대 학교 규정", description="학칙 등"),
                kb_item("club-ana", "KB_ANA", "clubs/ana/kb/", ["ana"], name="ANA 동아리 운영 자료"),
            ], "LastEvaluatedKey": {"kb_key": {"S": "club-ana"}}},
            {"Items": [
                kb_item("club-off", "KB_OFF", "clubs/off/kb/", ["ana"], enabled=False),
                kb_item("club-admin", "KB_ADMIN", "clubs/admin/kb/", ["admin"]),
            ]},
        ]

    def scan(self, **kw):
        assert kw["TableName"] == "registry-table"
        page = self.pages[self.scans % 2]
        self.scans += 1
        return page


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.errors = {}
        self.barrier = None
        self.results = {
            "KB_SCHOOL": [hit("school/kb/electric-heater__a004.md", SCHOOL_META, 0.61),
                          hit("school/kb/electric-heater__a006.md", SCHOOL_META, 0.60)],
            "KB_ANA": [hit("clubs/ana/kb/ana__1.md", CLUB_META, 0.65)],
            "KB_ADMIN": [hit("clubs/admin/kb/x.md", CLUB_META, 0.9)],
        }

    def retrieve(self, **kw):
        kb_id = kw["knowledgeBaseId"]
        self.calls.append((kb_id, kw["retrievalQuery"]["text"],
                           kw["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"]))
        if self.barrier:
            self.barrier.wait()  # 모든 KB 호출이 동시에 들어와야 통과합니다.
        if kb_id in self.errors:
            raise self.errors[kb_id]
        return {"retrievalResults": self.results.get(kb_id, [])}


class FakeS3:
    def __init__(self):
        self.calls = []
        self.objects = {
            "school/kb/electric-heater__a004.md": "# 제4조\n\n제4조(사용 승인범위) 본문".encode(),
            "school/kb/electric-heater__a004.md.metadata.json": json.dumps(
                {"metadataAttributes": SCHOOL_META}, ensure_ascii=False).encode(),
            "clubs/ana/kb/ana__1.md": "# 여름 2차 회의\n\n가을 MT 장소 후보".encode(),
        }

    def get_object(self, Bucket, Key, Range=None):
        assert Bucket == BUCKET
        self.calls.append(Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        data = self.objects[Key]
        resp = {"ContentLength": len(data)}
        if Range:
            end = int(Range.split("-")[1])
            resp["ContentRange"] = f"bytes 0-{min(end, len(data) - 1)}/{len(data)}"
            data = data[: end + 1]
            resp["ContentLength"] = len(data)
        resp["Body"] = io.BytesIO(data)
        return resp


@pytest.fixture
def fakes(monkeypatch):
    f = {"dynamodb": FakeDynamo(), "bedrock-agent-runtime": FakeRuntime(), "s3": FakeS3()}
    core.reset_cache()
    monkeypatch.setattr(core, "_client", lambda name: f[name])
    monkeypatch.setenv("REGISTRY_TABLE", "registry-table")
    monkeypatch.setenv("DOCS_BUCKET", BUCKET)
    yield f
    core.reset_cache()


# ---------------------------------------------------------------- 그룹 필터

def test_list_sources_ana_sees_public_and_ana(fakes):
    assert kb_search.list_sources(["ana"]) == [
        {"kb_key": "school", "name": "충남대 학교 규정", "description": "학칙 등"},
        {"kb_key": "club-ana", "name": "ANA 동아리 운영 자료", "description": ""},
    ]


@pytest.mark.parametrize("groups", [[], None, "ana", ["*"], ["other"]])
def test_list_sources_without_group_sees_public_only(fakes, groups):
    # 문자열 하나나 '*'를 그룹으로 넣어도 권한이 생기지 않아야 합니다.
    assert [s["kb_key"] for s in kb_search.list_sources(groups)] == ["school"]


def test_disabled_kb_is_hidden(fakes):
    keys = [s["kb_key"] for s in kb_search.list_sources(["ana", "admin"])]
    assert keys == ["school", "club-admin", "club-ana"]
    assert "club-off" not in keys


def test_registry_is_cached(fakes):
    kb_search.list_sources(["ana"])
    kb_search.search("전기난로", ["ana"])
    assert fakes["dynamodb"].scans == 2  # 첫 조회의 두 페이지만 읽고 다시 읽지 않음


# ---------------------------------------------------------------- 검색

def test_search_groups_results_by_kb(fakes):
    result = kb_search.search("  동아리방에서 전기난로를 써도 되나요?  ", ["ana"])
    assert result["query"] == "동아리방에서 전기난로를 써도 되나요?"
    assert result["skipped"] == []
    assert [g["kb_key"] for g in result["groups"]] == ["school", "club-ana"]
    school, club = result["groups"]
    assert school["name"] == "충남대 학교 규정"
    assert [d["score"] for d in school["results"]] == [0.61, 0.60]
    # 동아리 점수(0.65)가 더 높아도 학교 결과와 섞이지 않습니다.
    assert [d["doc_id"] for d in club["results"]] == ["clubs/ana/kb/ana__1.md"]
    assert "error" not in school and "error" not in club
    assert sorted(fakes["bedrock-agent-runtime"].calls) == [
        ("KB_ANA", "동아리방에서 전기난로를 써도 되나요?", 4),
        ("KB_SCHOOL", "동아리방에서 전기난로를 써도 되나요?", 4),
    ]


def test_search_calls_kbs_at_the_same_time(fakes):
    fakes["bedrock-agent-runtime"].barrier = threading.Barrier(2, timeout=5)
    result = kb_search.search("전기난로", ["ana"])
    assert all("error" not in g for g in result["groups"])


def test_search_sources_and_skipped(fakes):
    result = kb_search.search("전기난로", [], sources=["club-ana", "school", "nope", "club-off", "school"])
    assert [g["kb_key"] for g in result["groups"]] == ["school"]
    # 권한 없는 KB, 없는 KB, 꺼진 KB는 같은 방식으로 건너뜁니다 (있는지조차 알려 주지 않음).
    assert result["skipped"] == ["club-ana", "nope", "club-off"]
    assert [c[0] for c in fakes["bedrock-agent-runtime"].calls] == ["KB_SCHOOL"]


def test_search_sources_string_and_empty(fakes):
    assert [g["kb_key"] for g in kb_search.search("q", ["ana"], sources="club-ana")["groups"]] == ["club-ana"]
    assert [g["kb_key"] for g in kb_search.search("q", ["ana"], sources=[])["groups"]] == ["school", "club-ana"]


def test_search_without_allowed_kb_returns_empty(fakes):
    result = kb_search.search("q", [], sources=["club-ana"])
    assert result == {"query": "q", "groups": [], "skipped": ["club-ana"]}
    assert fakes["bedrock-agent-runtime"].calls == []


@pytest.mark.parametrize("per_kb, sent", [(50, 10), (0, 1), ("6", 6), ("x", 4)])
def test_per_kb_is_limited(fakes, per_kb, sent):
    kb_search.search("q", [], per_kb=per_kb)
    assert fakes["bedrock-agent-runtime"].calls == [("KB_SCHOOL", "q", sent)]


@pytest.mark.parametrize("query", ["", "   ", None, "가" * 1001])
def test_bad_query(fakes, query):
    with pytest.raises(kb_search.InvalidRequest):
        kb_search.search(query, ["ana"])
    assert fakes["bedrock-agent-runtime"].calls == []


def test_one_kb_failure_keeps_others(fakes):
    msg = "User: arn:aws:sts::123456789012:assumed-role/x is not authorized to perform bedrock:Retrieve"
    fakes["bedrock-agent-runtime"].errors["KB_ANA"] = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": msg}}, "Retrieve")
    result = kb_search.search("전기난로", ["ana"])
    school, club = result["groups"]
    assert len(school["results"]) == 2 and "error" not in school
    assert club == {"kb_key": "club-ana", "name": "ANA 동아리 운영 자료", "results": [],
                    "error": "AccessDeniedException"}
    assert "123456789012" not in json.dumps(result)


# ---------------------------------------------------------------- 메타데이터 → Doc

def test_school_doc_from_metadata(fakes):
    doc = kb_search.search("q", [])["groups"][0]["results"][0]
    assert doc == {
        "doc_id": "school/kb/electric-heater__a004.md",
        "kind": "school_rule",
        "title": "「충남대학교 전열기구 사용관리 규정」 제4조(사용 승인범위)",
        "detail": "제1042호, 2001-10-29 개정",
        "url": "https://www.law.go.kr/학칙공단/충남대학교전열기구사용관리규정",
        "text": "근거 문단",
        "score": 0.61,
    }
    assert BUCKET not in json.dumps(doc)  # 버킷 이름(계정 ID)이 문서 ID에 남지 않음


def test_club_doc_from_metadata(fakes):
    doc = kb_search.search("q", ["ana"], sources=["club-ana"])["groups"][0]["results"][0]
    assert doc["kind"] == "club_manual"
    assert doc["title"] == "전체 일정 > 일정 및 작업들 > 여름 2차 회의"
    assert doc["detail"] == "2026"
    assert doc["url"] == "https://www.notion.so/abc"


def test_describe_edge_cases():
    # 별표: 원문처럼 [별표 1]로, 제목 앞에 붙은 '별표'는 뗌
    byl = {"doc_type": "school_rule", "rule_name": "충남대학교 강의실 사용 허가에 관한 규정",
           "article_no": "별표 1", "article_title": "별표충남대학교 강의실 사용료", "rev_no": "제2111호"}
    assert core.describe(byl)[1:3] == ("「충남대학교 강의실 사용 허가에 관한 규정」 [별표 1] 충남대학교 강의실 사용료", "제2111호")
    # 연도가 없는 동아리 문서, 메타데이터가 없는 문서
    assert core.describe({"doc_type": "club_manual", "path": "전체 일정"}) == ("club_manual", "전체 일정", "", "")
    assert core.describe({}, "school/kb/a.md") == ("document", "a.md", "", "")


# ---------------------------------------------------------------- 문서 읽기

def test_read_document(fakes):
    doc = kb_search.read_document("school/kb/electric-heater__a004.md", [])
    assert doc == {
        "doc_id": "school/kb/electric-heater__a004.md",
        "kind": "school_rule",
        "title": "「충남대학교 전열기구 사용관리 규정」 제4조(사용 승인범위)",
        "detail": "제1042호, 2001-10-29 개정",
        "url": "https://www.law.go.kr/학칙공단/충남대학교전열기구사용관리규정",
        "text": "# 제4조\n\n제4조(사용 승인범위) 본문",
        "truncated": False,
    }


def test_read_document_without_metadata_uses_heading(fakes):
    doc = kb_search.read_document("clubs/ana/kb/ana__1.md", ["ana"])
    assert doc["title"] == "여름 2차 회의"
    assert doc["text"].endswith("가을 MT 장소 후보")


@pytest.mark.parametrize("doc_id, groups", [
    ("clubs/ana/kb/ana__1.md", []),                    # 권한 없는 KB
    ("clubs/ana/kb/ana__1.md", ["other"]),
    ("clubs/admin/kb/x.md", ["ana"]),
    ("school/raw/hakchik/source.html", ["ana"]),        # KB 폴더 밖
    ("_sources/cnu_rules.json", ["ana"]),
    ("school/kb/../raw/hakchik/source.html", ["ana"]),  # .. 로 빠져나가기
    ("../school/kb/a.md", ["ana"]),
    ("/school/kb/a.md", ["ana"]),
    ("school\\kb\\a.md", ["ana"]),
    ("school/kb//a.md", ["ana"]),
    ("school/kb/raw/a.md", ["ana"]),
    ("school/kb/a.md.metadata.json", ["ana"]),
    ("s3://bucket/school/kb/a.md", ["ana"]),
    ("", ["ana"]),
    (None, ["ana"]),
])
def test_read_document_rejects_outside_paths(fakes, doc_id, groups):
    with pytest.raises(kb_search.InvalidRequest):
        kb_search.read_document(doc_id, groups)
    assert fakes["s3"].calls == []


def test_read_document_missing(fakes):
    with pytest.raises(kb_search.InvalidRequest, match="찾지 못했습니다"):
        kb_search.read_document("school/kb/nope.md", [])


def test_read_document_truncates(fakes):
    fakes["s3"].objects["school/kb/long.md"] = ("가" * 30_000).encode()
    doc = kb_search.read_document("school/kb/long.md", [])
    assert len(doc["text"]) == 20_000
    assert doc["truncated"] is True
    # 딱 2만 자면 자르지 않은 것입니다.
    fakes["s3"].objects["school/kb/exact.md"] = ("a" * 20_000).encode()
    assert kb_search.read_document("school/kb/exact.md", [])["truncated"] is False
