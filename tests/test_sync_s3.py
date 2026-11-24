"""scripts/sync_s3.py의 계획 부분 단위 테스트. AWS를 부르지 않습니다."""

import pytest

import sync_s3


@pytest.mark.parametrize("target, expected", [
    ("school", "school/"),
    ("data/school/", "school/"),
    ("data\\clubs\\ana", "clubs/ana/"),
    ("_sources", "_sources/"),
])
def test_to_prefix(target, expected):
    assert sync_s3.to_prefix(target) == expected


@pytest.mark.parametrize("target", ["data", "", "../secret", ".git"])
def test_to_prefix_rejects(target):
    with pytest.raises(ValueError):
        sync_s3.to_prefix(target)


def test_kb_scope_picks_narrower_prefix():
    assert sync_s3.kb_scope("school/", "school/kb/") == "school/kb/"
    assert sync_s3.kb_scope("school/kb/", "school/kb/") == "school/kb/"
    assert sync_s3.kb_scope("_sources/", "school/kb/") is None
    assert sync_s3.kb_scope("clubs/", "clubs/ana/kb/") == "clubs/ana/kb/"


def test_plan_uploads_new_and_changed_only():
    local = {"school/kb/a.md": ("m1", 10), "school/kb/b.md": ("m2", 20), "school/raw/x.hwp": ("m3", 30)}
    remote = {"school/kb/a.md": ("m1", 10), "school/kb/b.md": ("old", 20)}
    uploads, deletes, blocked = sync_s3.plan_sync(local, remote, ["school/kb/"])
    assert uploads == ["school/kb/b.md", "school/raw/x.hwp"]
    assert deletes == [] and blocked == []


def test_plan_deletes_only_inside_kb_prefix():
    local = {"school/kb/a.md": ("m1", 10)}
    remote = {
        "school/kb/a.md": ("m1", 10),
        "school/kb/old.md": ("m9", 5),          # 다시 만든 조문 목록에서 빠짐 → 지움
        "school/raw/old.hwp": ("m8", 5),        # 원본은 쌓기만 → 남김
    }
    _, deletes, _ = sync_s3.plan_sync(local, remote, ["school/kb/"])
    assert deletes == ["school/kb/old.md"]


def test_plan_blocks_delete_when_local_kb_is_empty():
    local = {"school/raw/x.hwp": ("m3", 30)}
    remote = {"school/kb/a.md": ("m1", 10), "school/kb/b.md": ("m2", 20)}
    _, deletes, blocked = sync_s3.plan_sync(local, remote, ["school/kb/"])
    assert deletes == []
    assert blocked == ["school/kb/"]


def test_plan_multipart_etag_compares_size():
    local = {"_sources/big.tar.gz": ("md5", 100)}
    assert sync_s3.plan_sync(local, {"_sources/big.tar.gz": ("abc-3", 100)}, [])[0] == []
    assert sync_s3.plan_sync(local, {"_sources/big.tar.gz": ("abc-3", 99)}, [])[0] == ["_sources/big.tar.gz"]


def test_targets_for_keeps_data_paths_only(tmp_path):
    assert sync_s3.targets_for([sync_s3.DATA_ROOT / "school", sync_s3.DATA_ROOT / "school", tmp_path]) == ["school"]
    assert sync_s3.targets_for([sync_s3.DATA_ROOT]) == []
