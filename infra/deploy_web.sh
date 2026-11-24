#!/usr/bin/env bash
# 웹 챗봇 화면(web/)을 이 PC에서 웹 버킷에 올리고 CloudFront 캐시를 비웁니다.
# 평소에는 main에 push하면 GitHub Actions(.github/workflows/deploy.yml)가 앱 배포 뒤에 같은 방식으로 올립니다.
# 이 스크립트는 화면만 바로 고쳐 볼 때 씁니다. 앱(/ask)이 먼저 배포되어 있어야 합니다 (deploy_app.sh).
#   1) web/ 파일을 모으고 config.js를 만듭니다. API 주소는 앱 스택 출력, 나머지는 .state/outputs.env 값입니다.
#   2) 파일 형식(Content-Type)과 캐시 설정을 정해서 올리고, web/에 없는 파일은 버킷에서 지웁니다.
#   3) CloudFront 무효화가 끝날 때까지 기다린 뒤 웹 주소와 config.js를 확인합니다.
# deploy.yml의 웹 단계와 같은 방식입니다. 한쪽을 고치면 다른 쪽도 같이 고칩니다.
source "$(dirname "$0")/lib.sh"

for v in WEB_DISTRIBUTION_ID WEB_URL COGNITO_DOMAIN WEB_CLIENT_ID; do
  [ -n "${!v:-}" ] || die "$v 값이 없습니다. 09_web.sh와 07_cognito.sh를 먼저 실행하세요."
done
WEB_URL="${WEB_URL%/}"

API_URL="$(aws cloudformation describe-stacks --stack-name "$APP_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text 2>/dev/null || true)"
if [ -z "$API_URL" ] || [ "$API_URL" = "None" ]; then
  die "앱 스택의 ApiUrl을 읽지 못했습니다. deploy_app.sh를 먼저 실행하세요."
fi

# config.js에 넣는 값이 예상한 모양인지 확인합니다 (따옴표 등이 섞여 JS가 깨지지 않게).
check_value() { [[ "$2" =~ $3 ]] || die "$1 값의 모양이 이상합니다: $2"; }
check_value ApiUrl "$API_URL" '^https://[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com$'
check_value COGNITO_DOMAIN "$COGNITO_DOMAIN" '^[a-z0-9.-]+$'
check_value WEB_CLIENT_ID "$WEB_CLIENT_ID" '^[0-9a-z]{20,40}$'
check_value WEB_URL "$WEB_URL" '^https://[a-z0-9.-]+$'

log "1) web/ 파일 모으기와 config.js"
DIST="$TMP_DIR/web-dist"
rm -rf "$DIST"
mkdir -p "$DIST"
cp -R ../web/. "$DIST/"
# 시험 파일, 예시 설정(config.example.js), 로컬 개발용 config.js는 올리지 않습니다.
rm -f "$DIST"/config*.js "$DIST"/*.test.mjs
cat > "$DIST/config.js" <<EOF
// 배포 때 만든 파일입니다 (deploy.yml 또는 infra/deploy_web.sh). 고치지 마세요.
window.APP_CONFIG = {
  apiUrl: '${API_URL}',
  cognitoDomain: '${COGNITO_DOMAIN}',
  clientId: '${WEB_CLIENT_ID}',
  redirectUri: '${WEB_URL}/',
  mock: false,
};
EOF
ok "config.js (mock: false, 돌아갈 주소 ${WEB_URL}/)"

log "2) 웹 버킷에 올리기"
# 확장자로 형식을 정합니다. aws s3가 추측하게 두면 Windows에서는 .js가 text/plain이 되기도 하는데,
# 그러면 브라우저가 ES 모듈(app.js)을 실행하지 않습니다.
content_type() {
  case "$1" in
    *.html) echo 'text/html; charset=utf-8' ;;
    *.js | *.mjs) echo 'text/javascript; charset=utf-8' ;;
    *.css) echo 'text/css; charset=utf-8' ;;
    *.json) echo 'application/json' ;;
    *.svg) echo 'image/svg+xml' ;;
    *.png) echo 'image/png' ;;
    *.ico) echo 'image/x-icon' ;;
    *) return 1 ;;
  esac
}
# index.html, config.js: 늘 새로 받습니다. 나머지: 브라우저는 열 때마다 바뀌었는지 묻고(max-age=0),
# CloudFront는 하루 들고 있습니다(s-maxage). 배포 때마다 무효화하므로 바뀐 파일이 늦게 보이지 않습니다.
cache_control() {
  case "$1" in
    index.html | config.js) echo 'no-cache' ;;
    *) echo 'max-age=0, s-maxage=86400' ;;
  esac
}
upload() {   # 사용법: upload 파일(web/ 기준 경로)
  local ct
  ct="$(content_type "$1")" || die "형식을 모르는 파일입니다: web/$1 (content_type에 추가하세요)"
  aws s3 cp "$DIST/$1" "s3://${WEB_BUCKET}/$1" \
    --content-type "$ct" --cache-control "$(cache_control "$1")" --only-show-errors
  info "올림: $1 ($ct)"
}
# index.html은 다른 파일이 다 올라간 뒤 마지막에 올립니다 (새 화면이 아직 없는 파일을 찾지 않게).
mapfile -t FILES < <(cd "$DIST" && find . -type f ! -name index.html | sed 's|^\./||' | sort)
for f in "${FILES[@]}"; do upload "$f"; done
upload index.html
# web/에 없는 파일은 지웁니다. 방금 모두 올렸으므로 크기만 비교하면 다시 올리는 파일은 없습니다.
aws s3 sync "$DIST/" "s3://${WEB_BUCKET}/" --delete --size-only --no-progress
ok "웹 버킷에 올림"

log "3) CloudFront 무효화 (보통 1분 안)"
INV_ID="$(aws cloudfront create-invalidation --distribution-id "$WEB_DISTRIBUTION_ID" \
  --paths '/*' --query Invalidation.Id --output text)"
aws cloudfront wait invalidation-completed --distribution-id "$WEB_DISTRIBUTION_ID" --id "$INV_ID"
ok "무효화 $INV_ID 완료"

# 본문은 $TMP_DIR에 받습니다 (MSYS_NO_PATHCONV=1이라 curl에 /dev/null을 주면 쓰기 오류가 남).
code="$(curl -sS -o "$TMP_DIR/web-index.html" -w '%{http_code}' "${WEB_URL}/")"
[ "$code" = 200 ] || die "${WEB_URL}/ 이 HTTP $code 입니다."
curl -sS -o "$TMP_DIR/web-config.js" "${WEB_URL}/config.js"
cmp -s "$TMP_DIR/web-config.js" "$DIST/config.js" || die "CloudFront가 돌려준 config.js가 방금 만든 것과 다릅니다."
ok "${WEB_URL}/ 200, config.js 반영됨"

log "웹 주소: ${WEB_URL}/"
info "자세한 점검: bash check_web.sh"
