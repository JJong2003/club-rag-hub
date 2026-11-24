#!/usr/bin/env python3
"""국가법령정보센터(law.go.kr)에서 모은 충남대 규정 JSON을 S3 school/ 구조로 정리한다.

사용법:
    python scripts/prepare_school_kb.py <cnu_rules_lawgokr_YYYYMMDD.json> [출력 폴더] [--no-upload]
    (출력 폴더 기본값: data/school)
    끝나면 출력 폴더와 수집 원본 폴더를 S3 문서 버킷에 올리고 KB school을 동기화한다 (scripts/sync_s3.py).
    --no-upload를 주면 로컬에만 만든다.

출력 (S3 school/ 접두어와 같은 구조):
    raw/<rule_id>/source.html            law.go.kr 본문 응답 원본
    raw/<rule_id>/byl/<이름>.hwp         별표·별지 원본
    kb/<rule_id>__a007.md                조문 하나 = 파일 하나 (KB school, 청킹 NONE)
    kb/<rule_id>__a007.md.metadata.json  Bedrock KB 메타데이터
    kb/<rule_id>__byl01.md               별표 본문 (hwp5txt로 바꾼 경우만)
    manifest.json                        규정별 개정 번호, 파일 수 (rule-sync-state 참고용)

별표 변환에는 hwp5txt(pip install pyhwp)가 필요하다. 없으면 별표는 raw/에만 남긴다.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

# 규정 이름 → rule_id. rule_id는 S3 파일 이름과 DynamoDB rule-sync-state 키로 쓴다.
RULE_IDS = {
    "충남대학교 학칙": "hakchik",
    "충남대학교 화재예방 및 소방안전관리 규정": "fire-safety",
    "충남대학교 감염병 관리 규정": "infectious-disease",
    "충남대학교 전열기구 사용관리 규정": "electric-heater",
    "충남대학교 강의실 사용 허가에 관한 규정": "classroom-use",
    "충남대학교 정심화국제문화회관 운영규정": "jeongsimhwa-hall",
    "충남대학교 학생 포상 및 징계에 관한 규정": "student-award-discipline",
}

# KB에 넣지 않을 별표 (동아리 운영과 관계없는 학과·정원·학위 표). raw/에는 남긴다.
SKIP_BYL = {
    "hakchik": {"별표 2", "별표 9", "별표 10"},
}

MAX_BYL_CHARS = 5000  # 별표 한 파일 최대 글자 수. 넘으면 나눈다 (Titan V2 입력 한도 여유)
META_WARN_BYTES = 1500  # S3 Vectors 필터 메타데이터 2KB 한도 (KB가 붙이는 키 몫을 남김)

RE_HEADER = re.compile(
    r"\[시행\s*(?P<enf>[\d.\s]+)\]\s*\[(?P<kind>[^\]]*?)\s*(?P<no>제\d+호),\s*(?P<date>[\d.\s]+),\s*(?P<type>[^\]]+)\]"
)
RE_CHAPTER = re.compile(r"^제\d+(장|편)(\s|$)")
RE_SECTION = re.compile(r"^제\d+(절|관)(\s|$)")
RE_ARTICLE = re.compile(r"^제(?P<n>\d+)조(?:의(?P<sub>\d+))?\s*(?:\((?P<title>[^)]*)\))?")
RE_DELETED = re.compile(r"^\s*삭제\s*(<[^>]*>)?\s*$")
RE_BYL_NO = re.compile(r"^\[(?P<kind>별표|별지)\s*(?P<no>\d+(?:의\d+)?)\]\s*(?P<title>.*)$")


def iso_date(s: str) -> str:
    parts = [p for p in re.split(r"[.\s]+", s.strip()) if p]
    if len(parts) >= 3:
        y, m, d = parts[:3]
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    return s.strip()


def parse_header(header: str) -> dict:
    m = RE_HEADER.search(header or "")
    if not m:
        return {}
    return {
        "enforce_date": iso_date(m["enf"]),
        "rev_no": m["no"],
        "rev_date": iso_date(m["date"]),
        "rev_date_raw": re.sub(r"\s+", "", m["date"]),
        "rev_type": m["type"].strip(),
    }


def source_url(name: str) -> str:
    # 이름 주소는 항상 최신 개정본을 보여준다.
    return "https://www.law.go.kr/학칙공단/" + name.replace(" ", "")


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def file_ext(dl: dict) -> str:
    cd = unquote(dl.get("contentDisposition") or "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    if m and "." in m.group(1):
        return "." + m.group(1).rsplit(".", 1)[1].lower()
    ct = (dl.get("contentType") or "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "hwp" in ct:
        return ".hwp"
    return ".bin"


def safe_name(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", s).strip("_")
    return s[:80]


def _tool(name: str) -> str | None:
    exe = shutil.which(name)
    if exe:
        return exe
    cand = Path.home() / ".local/bin" / name
    return str(cand) if cand.exists() else None


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\r", " ")).strip()


def _text_no_tables(node) -> str:
    parts = [node.text or ""]
    for c in node:
        if _local(c.tag) != "table":
            parts.append(_text_no_tables(c))
        parts.append(c.tail or "")
    return "".join(parts)


def _cell_text(node) -> str:
    return _norm("".join(node.itertext())).replace("|", "/")


def _table_md(table) -> str:
    rows = [tr for tr in table.iter() if _local(tr.tag) == "tr"
            and _nearest_table(tr) is table]
    grid: list[list[str]] = []
    pending: dict[int, tuple[int, str]] = {}  # 열 → (남은 행 수, 글자)
    for tr in rows:
        row: list[str] = []
        cells = [c for c in tr if _local(c.tag) in ("td", "th")]
        ci = 0
        col = 0
        while ci < len(cells) or col in pending:
            if col in pending:
                left, txt = pending[col]
                row.append(txt)
                if left <= 1:
                    del pending[col]
                else:
                    pending[col] = (left - 1, txt)
                col += 1
                continue
            c = cells[ci]
            ci += 1
            txt = _cell_text(c)
            cs = int(c.get("colspan", "1") or 1)
            rs = int(c.get("rowspan", "1") or 1)
            for k in range(cs):
                row.append(txt if k == 0 else "")
                if rs > 1:
                    pending[col] = (rs - 1, txt if k == 0 else "")
                col += 1
        grid.append(row)
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    lines = ["| " + " | ".join(grid[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in grid[1:]]
    return "\n".join(lines)


_PARENT: dict = {}


def _nearest_table(node):
    p = _PARENT.get(node)
    while p is not None and _local(p.tag) != "table":
        p = _PARENT.get(p)
    return p


def _render(node, out: list[str]) -> None:
    tag = _local(node.tag)
    if tag == "table":
        md = _table_md(node)
        if md:
            out.append(md)
        return
    if tag == "p":
        txt = _norm(_text_no_tables(node))
        if txt:
            out.append(txt)
        for c in node:
            _render_tables_only(c, out)
        return
    for c in node:
        _render(c, out)


def _render_tables_only(node, out: list[str]) -> None:
    if _local(node.tag) == "table":
        _render(node, out)
        return
    for c in node:
        _render_tables_only(c, out)


def hwp_to_text(path: Path) -> str | None:
    """HWP를 글자로 바꾼다. 표는 마크다운 표로 남긴다 (hwp5html). 안 되면 hwp5txt."""
    import xml.etree.ElementTree as ET

    html_exe = _tool("hwp5html")
    if html_exe:
        with tempfile.TemporaryDirectory() as td:
            try:
                subprocess.run([html_exe, "--output", td, str(path)], check=True,
                               capture_output=True, timeout=120)
                root = ET.parse(Path(td) / "index.xhtml").getroot()
                _PARENT.clear()
                _PARENT.update({c: p for p in root.iter() for c in p})
                body = next((e for e in root.iter() if _local(e.tag) == "body"), root)
                out: list[str] = []
                _render(body, out)
                text = "\n".join(out).strip()
                if text:
                    return text
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ET.ParseError):
                pass
    txt_exe = _tool("hwp5txt")
    if not txt_exe:
        return None
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "out.txt"
        try:
            subprocess.run([txt_exe, "--output", str(out_path), str(path)], check=True,
                           capture_output=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
        text = out_path.read_text(encoding="utf-8", errors="replace")
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln) or None


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], []
    size = 0
    for ln in text.splitlines():
        if size + len(ln) + 1 > limit and cur:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


def meta_file(md_path: Path, attrs: dict, warnings: list[str]) -> None:
    attrs = {k: v for k, v in attrs.items() if v not in (None, "")}
    meta = {"metadataAttributes": attrs}
    size = len(json.dumps(attrs, ensure_ascii=False).encode("utf-8"))
    if size > META_WARN_BYTES:
        warnings.append(f"{md_path.name}: 메타데이터 {size}바이트")
    write_json(md_path.with_name(md_path.name + ".metadata.json"), meta)


def process_rule(reg: dict, out: Path, collected_at: str, warnings: list[str]) -> dict:
    name = reg["title"].strip()
    rule_id = RULE_IDS.get(name)
    if not rule_id:
        warnings.append(f"rule_id 없음, 건너뜀: {name}")
        return {}
    hdr = parse_header(reg.get("header", ""))
    url = source_url(name)
    version_url = f"https://www.law.go.kr/LSW/schlPubRulInfoP.do?schlPubRulSeq={reg['schlPubRulSeq']}"
    rev_label = f"{hdr.get('rev_no', '')}, {hdr.get('rev_date_raw', '')} {hdr.get('rev_type', '')}".strip(", ")

    raw_dir = out / "raw" / rule_id
    kb_dir = out / "kb"
    raw_dir.mkdir(parents=True, exist_ok=True)
    kb_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "source.html").write_text(reg.get("rawHtml", ""), encoding="utf-8", newline="\n")

    base_attrs = {
        "doc_type": "school_rule",
        "kb_key": "school",
        "rule_id": rule_id,
        "rule_name": name,
        "rev_no": hdr.get("rev_no"),
        "rev_date": hdr.get("rev_date"),
        "enforce_date": hdr.get("enforce_date"),
        "source_url": url,
        "source_site": "law.go.kr",
        "collected_at": collected_at,
    }

    chapter = section = ""
    articles, skipped_deleted, addenda = [], 0, 0
    seen = set()
    for item in reg.get("items", []):
        first = item.split("\n", 1)[0]
        if RE_CHAPTER.match(first):
            chapter, section = re.sub(r"\s+", " ", first).strip(), ""
            continue
        if RE_SECTION.match(first):
            section = re.sub(r"\s+", " ", first).strip()
            continue
        if first.startswith("부칙"):
            addenda += 1  # 부칙은 KB에 넣지 않는다 (raw/source.html에 남음)
            continue
        m = RE_ARTICLE.match(item)
        if not m:
            warnings.append(f"{rule_id}: 분류 못 한 항목: {first[:40]}")
            continue
        rest = item[m.end():]
        if RE_DELETED.match(rest):
            skipped_deleted += 1
            continue
        n, sub, title = int(m["n"]), m["sub"], (m["title"] or "").strip()
        art_no = f"제{n}조" + (f"의{sub}" if sub else "")
        key = f"a{n:03d}" + (f"-{int(sub)}" if sub else "")
        if key in seen:
            warnings.append(f"{rule_id}: 조문 번호 중복 {art_no}")
            key += "-dup"
        seen.add(key)
        where = " > ".join(x for x in (chapter, section) if x)
        heading = f"{name} {art_no}" + (f"({title})" if title else "")
        body = [f"# {heading}", ""]
        body.append(f"출처: 「{name}」 {art_no}" + (f"({title})" if title else "") + f" [{rev_label}]")
        if where:
            body.append(f"위치: {where}")
        body += ["", item.strip(), ""]
        md = kb_dir / f"{rule_id}__{key}.md"
        md.write_text("\n".join(body), encoding="utf-8", newline="\n")
        meta_file(md, {**base_attrs, "article_no": art_no, "article_title": title,
                       "chapter": where}, warnings)
        articles.append(md.name)

    byl_raw, byl_kb, byl_failed = [], [], []
    for b in reg.get("byl", []):
        bm = RE_BYL_NO.match(b.get("name", "").strip())
        label = f"{bm['kind']} {bm['no']}" if bm else b.get("name", "")[:20]
        btitle = bm["title"].strip() if bm else b.get("name", "")
        for i, dl in enumerate(b.get("downloads", [])):
            if not dl.get("b64"):
                continue
            suffix = f"_{i + 1}" if len(b["downloads"]) > 1 else ""
            fname = safe_name(f"{label}_{btitle}") + suffix + file_ext(dl)
            fpath = raw_dir / "byl" / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_bytes(base64.b64decode(dl["b64"]))
            byl_raw.append(fpath.name)
            if not bm or bm["kind"] != "별표" or label in SKIP_BYL.get(rule_id, set()):
                continue
            if fpath.suffix != ".hwp":
                continue
            text = hwp_to_text(fpath)
            if not text:
                byl_failed.append(fpath.name)
                continue
            parts = split_text(text, MAX_BYL_CHARS)
            main_no, _, sub_no = bm["no"].partition("의")
            no_key = f"{int(main_no):02d}" + (f"-{int(sub_no)}" if sub_no else "")
            for pi, part in enumerate(parts, 1):
                pkey = f"byl{no_key}" + (f"-p{pi}" if len(parts) > 1 else "")
                ptitle = f"[{label}] {btitle}" + (f" ({pi}/{len(parts)})" if len(parts) > 1 else "")
                md = kb_dir / f"{rule_id}__{pkey}.md"
                md.write_text("\n".join([
                    f"# {name} {ptitle}", "",
                    f"출처: 「{name}」 {ptitle} [{rev_label}]", "",
                    part, ""]), encoding="utf-8", newline="\n")
                meta_file(md, {**base_attrs, "article_no": label, "article_title": btitle}, warnings)
                byl_kb.append(md.name)

    return {
        "rule_id": rule_id,
        "rule_name": name,
        "schlPubRulSeq": reg["schlPubRulSeq"],
        **hdr,
        "source_url": url,
        "source_version_url": version_url,
        "department": reg.get("dept", ""),
        "articles": len(articles),
        "deleted_articles_skipped": skipped_deleted,
        "addenda_skipped": addenda,
        "byl_raw_files": len(byl_raw),
        "byl_kb_files": len(byl_kb),
        "byl_convert_failed": byl_failed,
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_upload = "--no-upload" in sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    src = Path(args[0])
    out = Path(args[1]) if len(args) > 1 else Path("data/school")
    payload = json.loads(src.read_text(encoding="utf-8"))
    collected_at = (payload.get("collectedAt") or datetime.now().isoformat())[:10]
    if (out / "kb").exists():
        shutil.rmtree(out / "kb")  # 지난 결과와 섞이지 않게 kb/만 새로 만든다
    out.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    rules = [r for r in (process_rule(reg, out, collected_at, warnings) for reg in payload["regs"]) if r]
    write_json(out / "manifest.json", {
        "source": payload.get("source"),
        "collected_at": payload.get("collectedAt"),
        "rules": rules,
        "warnings": warnings,
    })
    for r in rules:
        print(f"{r['rule_id']:<26} {r.get('rev_no', ''):<8} 조문 {r['articles']:>3}  "
              f"별표 KB {r['byl_kb_files']:>2}  원본 {r['byl_raw_files']:>2}"
              + (f"  변환 실패 {len(r['byl_convert_failed'])}" if r["byl_convert_failed"] else ""))
    for w in warnings:
        print("경고:", w)

    # 로컬에만 두지 않는다: 결과 폴더와 수집 원본을 S3에 올리고 KB school을 동기화한다.
    import sync_s3

    targets = sync_s3.targets_for([out, src.parent])
    if no_upload:
        print("\nS3에 올리지 않음 (--no-upload). 올리려면: python scripts/sync_s3.py " + " ".join(targets))
        return 0
    # 별표 변환이 실패한 채로 올리면 S3와 KB에 있던 별표 문서가 지워진다 (kb/는 로컬과 똑같이 맞추므로).
    failed = sum(len(r["byl_convert_failed"]) for r in rules)
    if failed:
        print(f"\n별표 {failed}개를 글자로 바꾸지 못해서 S3에 올리지 않았습니다. "
              "hwp5txt(pip install pyhwp)를 설치하고 다시 돌리거나, 별표 없이 올리려면: "
              "python scripts/sync_s3.py " + " ".join(targets))
        return 1
    print("\nS3에 올리는 중...")
    return sync_s3.sync(targets) if targets else 0


if __name__ == "__main__":
    sys.exit(main())
