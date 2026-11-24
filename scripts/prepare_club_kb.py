#!/usr/bin/env python3
"""Notion 내보내기(Markdown & CSV)를 S3 clubs/<동아리>/kb/ 구조로 정리한다.

사용법:
    python scripts/prepare_club_kb.py <압축 푼 Notion 내보내기 폴더> [출력 폴더] [동아리 키]
    (출력 폴더 기본값: data/clubs/ana, 동아리 키 기본값: ana)

하는 일:
    - 페이지(.md)마다 파일 하나로 만든다. KB club-ana는 FIXED_SIZE로 다시 자르므로 길이는 그대로 둔다.
    - 이미지·첨부 링크를 지우고, 내부 링크는 글자만 남긴다.
    - 전화번호, 메일, 계좌번호처럼 보이는 값, 학번은 [가림]으로 바꾼다.
    - 제목이나 경로에 EXCLUDE_KEYWORDS가 들어간 페이지는 건너뛴다 (계정·계좌·명단·개인 페이지 등).
    - 결과: kb/ana__<노션ID 앞 16자>.md + .metadata.json, manifest.json (건너뛴 페이지와 가린 개수 포함)

가림 규칙은 정규식이라 완벽하지 않다. 올리기 전에 manifest.json의 masked 목록을 훑어볼 것.
그래서 이 스크립트는 S3에 자동으로 올리지 않는다. 검토 후: python scripts/sync_s3.py data/clubs/ana
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

EXCLUDE_KEYWORDS = [
    "MAIN", "임원진 명단", "계좌", "계정", "비밀번호", "ANA 개인", "개인 페이지",
    "클라우드 아키텍처 텀프로젝트", "부원 명부", "연락처",
]
MIN_CHARS = 40  # 정리 후 이보다 짧은 페이지는 버린다

RE_ID = re.compile(r"\s([0-9a-f]{32})$")
RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
RE_LINK = re.compile(r"\[([^\]]+)\]\((?!https?://)[^)]*\)")
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
RE_PHONE = re.compile(r"(?<!\d)(01[016789]|0\d{1,2})[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
RE_ACCOUNT = re.compile(r"(?<![\d-])\d{2,6}-\d{2,6}-\d{2,8}(?:-\d{1,6})?(?![\d-])")
RE_ISO_DATE = re.compile(r"^(19|20)\d{2}-\d{1,2}-\d{1,2}$")
RE_STUDENT_NO = re.compile(r"(학번\s*[:：]?\s*)\d{8,10}")
RE_YEAR = re.compile(r"(20[1-3]\d)")


def page_id_and_title(p: Path) -> tuple[str, str]:
    m = RE_ID.search(p.stem)
    if m:
        return m.group(1), p.stem[: m.start()].strip()
    return "", p.stem.strip()


def clean_path_part(part: str) -> str:
    m = RE_ID.search(part)
    return part[: m.start()].strip() if m else part.strip()


def mask(text: str, counter: dict) -> str:
    def sub(rx, label, s, keep_group=None):
        def rep(m):
            if rx is RE_ACCOUNT and RE_ISO_DATE.match(m.group(0)):
                return m.group(0)
            counter[label] = counter.get(label, 0) + 1
            return (m.group(keep_group) if keep_group else "") + "[가림]"
        return rx.sub(rep, s)

    text = sub(RE_EMAIL, "email", text)
    text = sub(RE_STUDENT_NO, "student_no", text, keep_group=1)
    text = sub(RE_ACCOUNT, "account_or_phone", text)
    text = sub(RE_PHONE, "phone", text)
    return text


def tidy(md: str) -> str:
    md = RE_IMAGE.sub("", md)
    md = RE_LINK.sub(r"\1", md)
    lines = [ln.rstrip() for ln in md.splitlines()]
    out, blank = [], 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data/clubs/ana")
    club = sys.argv[3] if len(sys.argv) > 3 else "ana"
    kb = out / "kb"
    if kb.exists():
        shutil.rmtree(kb)
    kb.mkdir(parents=True)

    written, skipped = [], []
    for p in sorted(src.rglob("*.md")):
        rel_parts = [clean_path_part(x) for x in p.relative_to(src).parent.parts]
        pid, title = page_id_and_title(p)
        raw = p.read_text(encoding="utf-8", errors="replace")
        first = raw.lstrip().split("\n", 1)[0]
        if first.startswith("# ") and first[2:].strip():
            title = first[2:].strip()  # 파일 이름에 못 쓰는 글자(/, ? 등)가 든 원래 제목은 본문 첫 제목에 있다
        crumb = " > ".join([*rel_parts, title])
        if any(k in crumb for k in EXCLUDE_KEYWORDS):
            skipped.append({"path": crumb, "reason": "제외 키워드"})
            continue
        counter: dict = {}
        body = mask(tidy(raw), counter)
        if len(body) < MIN_CHARS:
            skipped.append({"path": crumb, "reason": "내용 거의 없음"})
            continue
        years = RE_YEAR.findall(crumb) or RE_YEAR.findall(body[:400])
        year = years[0] if years else ""
        key = (pid[:16] if pid else re.sub(r"\W+", "-", title)[:40]) or f"page{len(written)}"
        md_path = kb / f"{club}__{key}.md"
        if md_path.exists():
            md_path = kb / f"{club}__{key}-{len(written)}.md"
        header = [f"# {title}", "", f"출처: {club.upper()} 노션 / {crumb}" + (f" ({year})" if year else ""), ""]
        if body.startswith("# "):
            body = body.split("\n", 1)[1].lstrip() if "\n" in body else ""
        md_path.write_text("\n".join(header) + "\n" + body + "\n", encoding="utf-8", newline="\n")
        attrs = {
            "doc_type": "club_manual",
            "kb_key": f"club-{club}",
            "club": club,
            "title": title,
            "path": crumb[:200],
            "year": year,
            "source_site": "notion",
            "source_url": f"https://www.notion.so/{pid}" if pid else "",
            "collected_at": date.today().isoformat(),
        }
        attrs = {k: v for k, v in attrs.items() if v}
        md_path.with_name(md_path.name + ".metadata.json").write_text(
            json.dumps({"metadataAttributes": attrs}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        written.append({"file": md_path.name, "path": crumb, "chars": len(body), "masked": counter})

    (out / "manifest.json").write_text(json.dumps({
        "source": str(src), "club": club, "written": written, "skipped": skipped,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    masked_total = sum(sum(w["masked"].values()) for w in written)
    print(f"페이지 {len(written)}개 저장, {len(skipped)}개 건너뜀, 가림 {masked_total}건 → {kb}")
    # 개인정보가 남았을 수 있어서 자동으로 올리지 않는다. 검토한 뒤 직접 올린다.
    print(f"\n{out / 'manifest.json'}의 masked, skipped를 확인한 뒤 S3에 올리고 KB를 동기화하세요:\n"
          f"    python scripts/sync_s3.py {out.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
