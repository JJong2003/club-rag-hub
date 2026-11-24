#!/usr/bin/env bash
# 웹 챗봇 점검. 리소스를 만들거나 바꾸지 않습니다. (시험 계정으로 /ask를 한 번 불러서 Claude 호출 비용이 조금 듭니다)
#   1) 웹 주소: HTTPS 200, HTTP는 HTTPS로 넘김, 보안 헤더, 임시 페이지가 아닌 진짜 화면
#   2) 파일 형식: web/의 파일마다 Content-Type과 캐시 설정. 시험·예시 파일은 올라가 있지 않음
#   3) config.js: 가짜 응답(mock)이 아니고 실제 값(API 주소, Cognito 도메인, 웹 클라이언트, 돌아갈 주소)
#   4) S3 버킷 주소로 직접 열면 403 (CloudFront로만 읽힘)
#   5) /ask CORS: 웹 주소의 사전 요청은 허용, 다른 주소는 거부. 토큰 없이 부르면 401과 CORS 헤더
#   6) 시험 계정 토큰(check_mcp.sh와 같은 cli 클라이언트)으로 /ask → 200, 답변과 출처
#   7) Cognito: 웹 클라이언트·웹 주소로 로그인 화면까지 감, 등록 안 된 주소는 거부, 토큰 요청 CORS, 로그아웃 뒤 웹 주소로
# 토큰과 비밀번호는 출력하지 않습니다.
# pass와 bad는 항상 성공(0)을 돌려주므로 "조건 && pass || bad"가 if-else처럼 동작합니다.
# shellcheck disable=SC2015
source "$(dirname "$0")/lib.sh"

for v in WEB_URL WEB_DOMAIN COGNITO_DOMAIN WEB_CLIENT_ID USER_POOL_ID MCP_CLIENT_ID_CLI; do
  [ -n "${!v:-}" ] || die "$v 값이 없습니다. 09_web.sh와 07_cognito.sh를 먼저 실행하세요."
done
WEB_URL="${WEB_URL%/}"
TEST_USER_FILE=".state/mcp-test-user.env"
[ -f "$TEST_USER_FILE" ] || die "시험 계정 정보가 없습니다. 07_cognito.sh를 먼저 실행하세요."
# shellcheck source=/dev/null
source "$TEST_USER_FILE"

API_URL="$(aws cloudformation describe-stacks --stack-name "$APP_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)"
ASK_URL="${API_URL}/ask"

PY=python
fail=0
pass() { printf '  [통과] %s\n' "$*" >&2; }
bad()  { printf '  [실패] %s\n' "$*" >&2; fail=1; }

# curl -D로 받은 헤더 파일에서 값 하나를 꺼냅니다 (이름은 대소문자 무시). 사용법: header 파일 이름
header() { { grep -i "^$2:" "$1" || true; } | tail -1 | cut -d: -f2- | sed 's/^ *//' | tr -d '\r'; }
# URL 쿼리에 넣을 주소를 인코딩합니다 (: 와 /만 있는 주소용).
enc() { local s="${1//:/%3A}"; printf '%s' "${s//\//%2F}"; }

# ---------------------------------------------------------------- 1
log "1) 웹 주소 ${WEB_URL}/"
H="$TMP_DIR/web-index.headers"
code="$(curl -sS -o "$TMP_DIR/web-index.html" -D "$H" -w '%{http_code}' "${WEB_URL}/")"
[ "$code" = 200 ] && pass "HTTPS 200" || bad "HTTPS $code (200이어야 함)"
code="$(curl -sS -o "$TMP_DIR/web-http.html" -D "$TMP_DIR/web-http.headers" -w '%{http_code}' "http://${WEB_DOMAIN}/")"
loc="$(header "$TMP_DIR/web-http.headers" location)"
[ "$code" = 301 ] && [ "$loc" = "${WEB_URL}/" ] && pass "HTTP는 HTTPS로 넘김 (301)" || bad "HTTP 요청: $code, Location: $loc"
# CloudFront 관리형 응답 헤더 정책(SecurityHeadersPolicy)이 붙이는 헤더
for h in strict-transport-security x-content-type-options x-frame-options referrer-policy; do
  v="$(header "$H" "$h")"
  [ -n "$v" ] && pass "$h: $v" || bad "$h 헤더 없음"
done
grep -q 'id="login-button"' "$TMP_DIR/web-index.html" && pass "웹 챗봇 화면 (로그인 버튼 있음)" \
  || bad "index.html이 웹 챗봇 화면이 아닙니다 (09_web.sh의 임시 페이지?). deploy_web.sh를 실행하세요."

# ---------------------------------------------------------------- 2
log "2) 파일 형식과 캐시 (web/ 기준)"
expect_type() {
  case "$1" in
    *.html) echo '^text/html' ;;
    *.js | *.mjs) echo '^(text|application)/javascript' ;;   # ES 모듈은 JavaScript 형식이어야 실행됨
    *.css) echo '^text/css' ;;
    *) echo '.' ;;
  esac
}
for path in ../web/*; do
  f="$(basename "$path")"
  hf="$TMP_DIR/web-file.headers"
  code="$(curl -sS -o "$TMP_DIR/web-file.body" -D "$hf" -w '%{http_code}' "${WEB_URL}/${f}")"
  case "$f" in
    config.example.js | *.test.mjs | config.js) ;;   # 아래에서 따로 봅니다
    *)
      ct="$(header "$hf" content-type)"
      cc="$(header "$hf" cache-control)"
      [ "$code" = 200 ] && printf '%s' "$ct" | grep -qiE "$(expect_type "$f")" \
        && pass "$f: $ct / cache-control: ${cc:-(없음)}" || bad "$f: HTTP $code, Content-Type: $ct"
      ;;
  esac
  case "$f" in
    config.example.js | *.test.mjs)
      [ "$code" != 200 ] && pass "$f 안 올라감 (HTTP $code)" || bad "$f가 올라가 있습니다 (올리지 않는 파일)"
      ;;
  esac
done
idx_cc="$(header "$H" cache-control)"
[ "$idx_cc" = no-cache ] && pass "index.html 캐시 안 함 (no-cache)" || bad "index.html cache-control: $idx_cc (no-cache여야 함)"

# ---------------------------------------------------------------- 3
log "3) config.js"
hf="$TMP_DIR/web-config.headers"
code="$(curl -sS -o "$TMP_DIR/web-config.js" -D "$hf" -w '%{http_code}' "${WEB_URL}/config.js")"
ct="$(header "$hf" content-type)"
cc="$(header "$hf" cache-control)"
[ "$code" = 200 ] && printf '%s' "$ct" | grep -qiE '^(text|application)/javascript' \
  && pass "config.js: $ct" || bad "config.js: HTTP $code, Content-Type: $ct"
[ "$cc" = no-cache ] && pass "config.js 캐시 안 함 (no-cache)" || bad "config.js cache-control: $cc (no-cache여야 함)"
# 키마다 "이름<TAB>ok|bad" 한 줄씩 출력합니다. 값은 출력하지 않습니다.
# (Windows의 Python은 줄 끝에 \r을 붙이므로 지웁니다)
CFG_RESULT="$($PY - "$TMP_DIR/web-config.js" "$API_URL" "$COGNITO_DOMAIN" "$WEB_CLIENT_ID" "${WEB_URL}/" <<'PY' | tr -d '\r'
import re, sys
src = open(sys.argv[1], encoding="utf-8").read()
cfg = dict(re.findall(r"^\s*(\w+):\s*'([^']*)'", src, re.M))
m = re.search(r"^\s*mock:\s*(\w+)", src, re.M)
cfg["mock"] = m.group(1) if m else "(없음)"
want = {"apiUrl": sys.argv[2], "cognitoDomain": sys.argv[3], "clientId": sys.argv[4],
        "redirectUri": sys.argv[5], "mock": "false"}
for k, v in want.items():
    print(f"{k}\t{'ok' if cfg.get(k) == v else 'bad'}")
PY
)" || CFG_RESULT=$'config.js\tbad'
while IFS=$'\t' read -r k r; do
  [ "$r" = ok ] && pass "config.js $k 값이 맞음" || bad "config.js $k 값이 다름 (deploy_web.sh를 다시 실행하세요)"
done <<< "$CFG_RESULT"

# ---------------------------------------------------------------- 4
log "4) S3 버킷 주소로 직접 열기"
code="$(curl -sS -o "$TMP_DIR/web-s3.body" -w '%{http_code}' "https://${WEB_BUCKET}.s3.${AWS_REGION}.amazonaws.com/index.html")"
[ "$code" = 403 ] && pass "403 (CloudFront로만 읽힘)" || bad "HTTP $code (403이어야 함)"

# ---------------------------------------------------------------- 5
log "5) /ask CORS와 로그인 확인"
# 사전 요청(OPTIONS)을 보내고 응답의 허용 Origin을 출력합니다. 사용법: preflight Origin
preflight() {
  curl -sS -o "$TMP_DIR/ask-preflight.body" -D "$TMP_DIR/ask-preflight.headers" -X OPTIONS "$ASK_URL" \
    -H "Origin: $1" -H 'Access-Control-Request-Method: POST' \
    -H 'Access-Control-Request-Headers: authorization,content-type'
  header "$TMP_DIR/ask-preflight.headers" access-control-allow-origin
}
got="$(preflight "$WEB_URL")"
allow_headers="$(header "$TMP_DIR/ask-preflight.headers" access-control-allow-headers)"
[ "$got" = "$WEB_URL" ] && pass "웹 주소의 사전 요청 허용 (allow-headers: $allow_headers)" || bad "웹 주소의 사전 요청: allow-origin '$got'"
printf '%s' "$allow_headers" | grep -qi authorization && pass "Authorization 헤더 허용" || bad "allow-headers에 authorization 없음"
got="$(preflight https://evil.example.com)"
[ -z "$got" ] && pass "다른 주소(https://evil.example.com)는 허용 헤더 없음 → 브라우저가 막음" || bad "다른 주소도 허용됨: $got"

hf="$TMP_DIR/ask-401.headers"
code="$(printf '%s' '{"question":"check"}' | curl -sS -o "$TMP_DIR/ask-401.body" -D "$hf" -w '%{http_code}' \
  -X POST "$ASK_URL" -H "Origin: ${WEB_URL}" -H 'Content-Type: application/json' --data-binary @-)"
[ "$code" = 401 ] && pass "토큰 없이 POST /ask → 401" || bad "토큰 없이 POST /ask → $code (401이어야 함)"
[ "$(header "$hf" access-control-allow-origin)" = "$WEB_URL" ] && pass "401에도 CORS 헤더 (화면이 401을 읽고 다시 로그인 안내)" \
  || bad "401 응답에 웹 주소 CORS 헤더 없음"

# ---------------------------------------------------------------- 6
log "6) 시험 계정 토큰으로 /ask"
TOKEN="$(aws cognito-idp admin-initiate-auth --user-pool-id "$USER_POOL_ID" --client-id "$MCP_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=${MCP_TEST_USER},PASSWORD=${MCP_TEST_PASSWORD}" \
  --query AuthenticationResult.AccessToken --output text)"
[ -n "$TOKEN" ] && [ "$TOKEN" != "None" ] && pass "토큰 받음 ($MCP_TEST_USER)" || die "토큰을 받지 못했습니다."
# 한글 본문은 파일로 넘깁니다 (Windows에서 명령줄 인자로 넘기면 깨짐).
printf '%s' '{"question":"동아리방에서 전기난로를 써도 되나요?","mode":"qa"}' > "$TMP_DIR/ask-question.json"
code="$(curl -sS -o "$TMP_DIR/ask-200.json" -D "$TMP_DIR/ask-200.headers" -w '%{http_code}' --max-time 40 \
  -X POST "$ASK_URL" -H "Authorization: Bearer ${TOKEN}" -H "Origin: ${WEB_URL}" \
  -H 'Content-Type: application/json' --data-binary "@${TMP_DIR}/ask-question.json")"
[ "$code" = 200 ] && pass "POST /ask → 200" || bad "POST /ask → $code: $(head -c 300 "$TMP_DIR/ask-200.json")"
if [ "$code" = 200 ]; then
  n_sources="$($PY - "$TMP_DIR/ask-200.json" <<'PY' | tr -d '\r'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
src = d.get("sources") or []
lines = [f"  answer: {' '.join(str(d.get('answer') or '').split())[:120]}…",
         f"  sources {len(src)}개, searched {d.get('searched')}, no_answer {d.get('no_answer')}, {d.get('ms')}ms"]
lines += [f"  [{s.get('n')}] {s.get('title')} ({s.get('detail')})" for s in src[:3]]
sys.stderr.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
print(len(src) if d.get("answer") and not d.get("no_answer") else 0)
PY
)" || n_sources=0
  [ "${n_sources:-0}" -gt 0 ] && pass "답변과 출처 ${n_sources}개" || bad "답변이 없거나 출처가 없음"
fi

# ---------------------------------------------------------------- 7
log "7) Cognito 로그인 화면 (웹 앱 클라이언트)"
CHALLENGE="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"   # 아무 값이나 되는 PKCE 값 (RFC 7636 예시)
authorize_url() {   # 사용법: authorize_url 돌아갈주소
  printf 'https://%s/oauth2/authorize?response_type=code&client_id=%s&redirect_uri=%s&scope=openid+email+profile&state=check&code_challenge=%s&code_challenge_method=S256' \
    "$COGNITO_DOMAIN" "$WEB_CLIENT_ID" "$(enc "$1")" "$CHALLENGE"
}
res="$(curl -sS -L -o "$TMP_DIR/cognito-login.html" -w '%{http_code} %{url_effective}' "$(authorize_url "${WEB_URL}/")")"
code="${res%% *}" final="${res#* }"
final_path="${final#https://"${COGNITO_DOMAIN}"}"
final_path="${final_path%%\?*}"
[ "$code" = 200 ] && [ "$final_path" = /login ] && grep -q 'name="password"' "$TMP_DIR/cognito-login.html" \
  && pass "authorize → 로그인 화면(/login) 200, 비밀번호 칸 있음" || bad "authorize 결과: HTTP $code, 경로 $final_path"

curl -sS -o "$TMP_DIR/cognito-bad.html" -D "$TMP_DIR/cognito-bad.headers" "$(authorize_url https://evil.example.com/)"
loc="$(header "$TMP_DIR/cognito-bad.headers" location)"
printf '%s' "$loc" | grep -q 'error=redirect_mismatch' && pass "등록 안 된 돌아갈 주소는 거부 (redirect_mismatch)" \
  || bad "등록 안 된 돌아갈 주소가 거부되지 않음"

curl -sS -o "$TMP_DIR/cognito-token.body" -D "$TMP_DIR/cognito-token.headers" -X OPTIONS "https://${COGNITO_DOMAIN}/oauth2/token" \
  -H "Origin: ${WEB_URL}" -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: content-type'
[ "$(header "$TMP_DIR/cognito-token.headers" access-control-allow-origin)" = "$WEB_URL" ] \
  && pass "토큰 요청 사전 요청 허용 (브라우저가 코드를 토큰으로 바꿀 수 있음)" || bad "토큰 요청 CORS가 웹 주소를 허용하지 않음"

curl -sS -o "$TMP_DIR/cognito-logout.body" -D "$TMP_DIR/cognito-logout.headers" \
  "https://${COGNITO_DOMAIN}/logout?client_id=${WEB_CLIENT_ID}&logout_uri=$(enc "${WEB_URL}/")"
loc="$(header "$TMP_DIR/cognito-logout.headers" location)"
[ "$loc" = "${WEB_URL}/" ] && pass "로그아웃 뒤 웹 주소로 돌아옴" || bad "로그아웃 뒤 이동 주소: $loc"

echo >&2
[ "$fail" = 0 ] && log "모두 통과" || { log "실패한 항목이 있습니다"; exit 1; }
