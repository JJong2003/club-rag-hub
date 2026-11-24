"""로컬 확인 명령: 배포 없이 실제 AWS로 검색 결과를 봅니다.

저장소 루트에서 (Git Bash):
  export PYTHONPATH=src AWS_PROFILE=club-rag-hub AWS_REGION=ap-northeast-2
  python -m kb_search "동아리방에서 전기난로를 써도 되나요?" --groups ana
  python -m kb_search "질문" --sources school --per-kb 6     학교 규정만, 6개씩
  python -m kb_search --list --groups ana                    볼 수 있는 KB 목록
  python -m kb_search --read school/kb/electric-heater__a004.md --groups ana   문서 전체
  --json을 붙이면 함수가 돌려준 그대로(JSON) 출력합니다.

--groups를 빼면 그룹이 없는 사람(모두에게 열린 KB만)으로 검색합니다.
"""

import argparse
import json
import re
import sys
import time

from . import core


def _split(values):
    return [v.strip() for item in values or [] for v in item.split(",") if v.strip()]


def _snippet(text, size=160):
    # 학교 조문은 '# 제목'과 '출처:' 줄로 시작하므로 빼고 본문 앞부분만 보여 줍니다.
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith(("# ", "출처:"))]
    flat = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return flat if len(flat) <= size else flat[:size] + "…"


def print_search(result, groups):
    print(f"질문: {result['query']}")
    print(f"그룹: {', '.join(groups) or '(없음)'}")
    for g in result["groups"]:
        status = f"오류 {g['error']}" if "error" in g else f"{len(g['results'])}건"
        print(f"\n[{g['kb_key']}] {g['name']} - {status}")
        for i, d in enumerate(g["results"], 1):
            print(f"  {i}. {d['score']:.3f}  {d['title']}" + (f"  ({d['detail']})" if d["detail"] else ""))
            print(f"     {d['doc_id']}")
            if d["url"]:
                print(f"     {d['url']}")
            print(f"     {_snippet(d['text'])}")
    if result["skipped"]:
        print(f"\n건너뜀 (권한이 없거나 없는 KB): {', '.join(result['skipped'])}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m kb_search", description="kb_search 로컬 확인 (실제 AWS 호출)")
    p.add_argument("query", nargs="?", help="검색할 질문")
    p.add_argument("--groups", action="append", help="호출자 그룹, 쉼표로 구분 (예: ana)")
    p.add_argument("--sources", action="append", help="검색할 KB, 쉼표로 구분 (예: school)")
    p.add_argument("--per-kb", type=int, default=core.DEFAULT_PER_KB, help="KB마다 가져올 개수 (최대 10)")
    p.add_argument("--list", action="store_true", help="볼 수 있는 KB 목록")
    p.add_argument("--read", metavar="DOC_ID", help="문서 전체 읽기")
    p.add_argument("--json", action="store_true", help="결과를 JSON 그대로 출력")
    args = p.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # Windows 파이프에서 한글이 깨지지 않게
    groups = _split(args.groups)
    start = time.perf_counter()
    try:
        if args.list:
            out = core.list_sources(groups)
        elif args.read:
            out = core.read_document(args.read, groups)
        elif args.query:
            out = core.search(args.query, groups, _split(args.sources) or None, args.per_kb)
        else:
            p.error("질문을 적거나 --list, --read 중 하나를 쓰세요.")
    except core.InvalidRequest as e:
        print(f"입력 오류: {e}", file=sys.stderr)
        return 2
    ms = round((time.perf_counter() - start) * 1000)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.list:
        for s in out:
            print(f"[{s['kb_key']}] {s['name']}: {s['description']}")
    elif args.read:
        print(f"{out['title']}" + (f"  ({out['detail']})" if out["detail"] else ""))
        print(f"{out['doc_id']}  {out['url']}")
        print(f"{len(out['text']):,}자" + (" (2만 자에서 자름)" if out["truncated"] else "") + "\n")
        print(out["text"])
    else:
        print_search(out, groups)
    print(f"\n({ms:,}ms)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
