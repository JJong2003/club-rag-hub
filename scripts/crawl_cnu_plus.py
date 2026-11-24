#!/usr/bin/env python3
"""충남대 홈페이지(plus.cnu.ac.kr) 학칙규정 게시판을 훑어서 목록과 원본 파일을 받는다.

사용법 (저장소 루트, 학교 사이트에 접속되는 PC에서):
    python scripts/crawl_cnu_plus.py                 # 목록 + 기본 대상(상품권·홍보게시물·학생회칙) 받기
    python scripts/crawl_cnu_plus.py --all           # 목록에 있는 규정 전부 받기
    python scripts/crawl_cnu_plus.py --match 강의실   # 이름에 '강의실'이 들어간 규정 받기 (여러 번 줄 수 있음)

결과 (data/_sources/cnu_plus/):
    catalog.json                 규정 목록 (mng_no, 편, 규정명, 담당 부서)
    <mng_no>/view.html           규정 보기 화면 원본 (개정 이력 표 포함)
    <mng_no>/<원본 파일>          HWP/PDF
    debug/                       구조가 예상과 다를 때 받은 화면 (Claude에게 보여줄 것)
끝나면 debug/를 뺀 결과를 S3 _sources/cnu_plus/에 올린다 (--no-upload로 끄기).

robots.txt가 막고 있으면 내용을 보여주고 멈춘다. 확인한 뒤에도 돌리려면 --ignore-robots를 준다.
요청 사이에 1초씩 쉰다. 표준 라이브러리만 쓴다 (Lambda로 옮기기 쉽게).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

BASE = "https://plus.cnu.ac.kr"
LIST_PATH = "/_prog/rule/"
VIEW_PATH = "/_prog/rule/view_pop.php"
UA = "club-rag-hub/0.1 (CNU ANA club term project; weekly rule check)"
DEFAULT_TARGETS = ["상품권", "홍보게시물", "학생회칙"]
GUBUNS = ["", "1", "2", "3", "4", "5", "6", "7", "8"]  # ""는 전체 목록
DELAY = 1.0

RE_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
RE_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
RE_MNG = re.compile(r"mng_no\D{0,6}(\d+)", re.I)
RE_TAG = re.compile(r"<[^>]+>")
RE_ATTR = re.compile(r"""(?:href|onclick)\s*=\s*(["'])(.*?)\1""", re.I | re.S)
RE_FILE_HINT = re.compile(r"\.hwpx?|\.pdf|download|down\.php|file_?down|atch", re.I)
RE_JS_ARG = re.compile(r"""['"]([^'"]*[/.=][^'"]*)['"]""")


def file_links(page_html: str) -> list[str]:
    links = []
    for _, val in RE_ATTR.findall(page_html):
        val = html.unescape(val).strip()
        if not RE_FILE_HINT.search(val):
            continue
        if val.lower().startswith("javascript") or ("(" in val and not val.startswith(("/", "http", "."))):
            m = RE_JS_ARG.search(val)
            if not m:
                continue
            val = m.group(1)
        links.append(val)
    return list(dict.fromkeys(links))


def text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(RE_TAG.sub(" ", s))).strip()


class Client:
    def __init__(self) -> None:
        self.last = 0.0

    def get(self, url: str, referer: str | None = None) -> tuple[bytes, dict]:
        wait = DELAY - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Referer": referer} if referer else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                headers = {k.lower(): v for k, v in r.headers.items()}
        finally:
            self.last = time.time()
        return body, headers


def decode(body: bytes, headers: dict) -> str:
    m = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "euc-kr", "cp949"]:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def filename_from(headers: dict, fallback: str) -> str:
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        name = urllib.parse.unquote(m.group(1))
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd)
        name = m.group(1) if m else fallback
        name = urllib.parse.unquote(name)
        try:  # 한글 이름이 EUC-KR/UTF-8 바이트 그대로 오는 경우
            raw = name.encode("latin-1")
            for enc in ("utf-8", "cp949"):
                try:
                    name = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    pass
        except UnicodeEncodeError:
            pass
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or fallback
    return name[:150]


def check_robots(ignore: bool) -> None:
    rp = urllib.robotparser.RobotFileParser(BASE + "/robots.txt")
    try:
        req = urllib.request.Request(BASE + "/robots.txt", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            lines = r.read().decode("utf-8", errors="replace").splitlines()
    except urllib.error.URLError as e:
        print(f"robots.txt를 못 읽음 ({e}). 계속 진행합니다.")
        return
    rp.parse(lines)
    allowed = rp.can_fetch(UA, BASE + LIST_PATH) and rp.can_fetch(UA, BASE + VIEW_PATH)
    if allowed:
        print("robots.txt: 학칙규정 경로 허용")
        return
    print("robots.txt가 이 경로의 자동 수집을 막고 있습니다:")
    print("\n".join("    " + ln for ln in lines[:40]))
    if not ignore:
        print("\n내용을 확인하고 그래도 받으려면 --ignore-robots를 붙여 다시 실행하세요.")
        sys.exit(3)
    print("--ignore-robots: 1초 간격으로 진행합니다.\n")


def parse_list(page_html: str) -> list[dict]:
    rows = []
    for tr in RE_TR.findall(page_html):
        m = RE_MNG.search(tr)
        if not m:
            continue
        cells = [text(c) for c in RE_CELL.findall(tr)]
        name = max(cells, key=len) if cells else ""
        rows.append({"mng_no": m.group(1), "cells": cells, "name": name})
    return rows


def crawl_catalog(cli: Client, out: Path) -> dict[str, dict]:
    catalog: dict[str, dict] = {}
    for g in GUBUNS:
        page = 1
        while page <= 60:
            q = {"site_dvs_cd": "kr", "menu_dvs_cd": "0605", "GotoPage": str(page)}
            if g:
                q["gubun"] = g
            url = BASE + LIST_PATH + "?" + urllib.parse.urlencode(q)
            try:
                body, hdr = cli.get(url)
            except urllib.error.URLError as e:
                print(f"  목록 요청 실패 {url}: {e}")
                break
            page_html = decode(body, hdr)
            rows = parse_list(page_html)
            new = [r for r in rows if r["mng_no"] not in catalog]
            if page == 1 and g == "" and not rows:
                dbg = out / "debug"
                dbg.mkdir(parents=True, exist_ok=True)
                (dbg / "list_page1.html").write_text(page_html, encoding="utf-8", newline="\n")
                print("목록에서 규정을 하나도 못 찾았습니다. data/_sources/cnu_plus/debug/list_page1.html 을 Claude에게 보여주세요.")
                sys.exit(4)
            for r in new:
                catalog[r["mng_no"]] = {**r, "gubun": g}
            print(f"  편 {g or '전체'} 페이지 {page}: {len(rows)}건 (새 {len(new)}건)")
            if not new:
                break
            page += 1
    return catalog


def download_rule(cli: Client, out: Path, item: dict) -> dict:
    view_url = f"{BASE}{VIEW_PATH}?mng_no={item['mng_no']}"
    d = out / item["mng_no"]
    d.mkdir(parents=True, exist_ok=True)
    body, hdr = cli.get(view_url, referer=BASE + LIST_PATH)
    view_html = decode(body, hdr)
    (d / "view.html").write_text(view_html, encoding="utf-8", newline="\n")
    files, failed = [], []
    for href in file_links(view_html):
        url = urllib.parse.urljoin(view_url, href)
        if not url.startswith(BASE):
            continue
        try:
            fbody, fhdr = cli.get(url, referer=view_url)
        except urllib.error.URLError as e:
            failed.append({"url": url, "error": str(e)})
            continue
        ctype = fhdr.get("content-type", "")
        if "text/html" in ctype and b"<html" in fbody[:2000].lower():
            failed.append({"url": url, "error": "파일 대신 HTML이 옴"})
            continue
        name = filename_from(fhdr, Path(urllib.parse.urlparse(url).path).name or "file.bin")
        (d / name).write_bytes(fbody)
        files.append({"name": name, "url": url, "bytes": len(fbody)})
    return {"mng_no": item["mng_no"], "name": item["name"], "view_url": view_url,
            "files": files, "failed": failed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/_sources/cnu_plus")
    ap.add_argument("--match", action="append", help="규정 이름에 이 글자가 들어간 것만 받기")
    ap.add_argument("--all", action="store_true", help="목록 전체 받기")
    ap.add_argument("--ignore-robots", action="store_true")
    ap.add_argument("--no-upload", action="store_true", help="받은 파일을 S3에 올리지 않기")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    check_robots(args.ignore_robots)
    cli = Client()

    print("목록 수집 중...")
    catalog = crawl_catalog(cli, out)
    (out / "catalog.json").write_text(json.dumps(list(catalog.values()), ensure_ascii=False, indent=2),
                                      encoding="utf-8", newline="\n")
    print(f"규정 {len(catalog)}건 → {out / 'catalog.json'}")

    keys = args.match or DEFAULT_TARGETS
    targets = list(catalog.values()) if args.all else [
        r for r in catalog.values() if any(k in r["name"] for k in keys)]
    missing = [] if args.all else [k for k in keys if not any(k in r["name"] for r in catalog.values())]
    print(f"받을 규정 {len(targets)}건" + (f", 목록에 없는 검색어: {missing}" if missing else ""))

    results = []
    for item in targets:
        r = download_rule(cli, out, item)
        results.append(r)
        status = ", ".join(f["name"] for f in r["files"]) or "파일 링크 못 찾음 (view.html 확인)"
        print(f"  [{item['mng_no']}] {item['name']}: {status}")
    (out / "downloads.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                        encoding="utf-8", newline="\n")

    # 받은 원본은 로컬에만 두지 않고 S3 _sources/cnu_plus/에 쌓는다 (debug/는 빼고). KB 문서가 아니라 동기화는 없음.
    if args.no_upload:
        return 0
    import sync_s3  # boto3가 필요해서 크롤링이 끝난 뒤에 부른다

    print("\nS3에 올리는 중...")
    return sync_s3.sync(sync_s3.targets_for([out]), ingest="never")


if __name__ == "__main__":
    sys.exit(main())
