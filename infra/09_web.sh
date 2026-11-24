#!/usr/bin/env bash
# 09. 웹 호스팅: 웹 챗봇 화면(정적 파일)을 둘 S3 버킷과 CloudFront 배포
#     - 버킷은 비공개이고 CloudFront(OAC)만 읽습니다. 사용자는 CloudFront 주소(HTTPS)로만 들어옵니다.
#     - GitHub 배포 역할에 CloudFront를 만드는 권한을 주지 않으려고 SAM이 아니라 이 스크립트로 만듭니다.
#     - 07_cognito.sh의 웹 앱 클라이언트가 이 주소(WEB_URL)를 돌아갈 주소로 쓰므로 07보다 먼저 실행합니다.
#
# 처음 만들 때 확인용 임시 index.html을 올립니다. 진짜 화면(web/)은 배포 단계가 올리면서 덮어씁니다.
source "$(dirname "$0")/lib.sh"

OAC_NAME="${PREFIX}-web"
DIST_COMMENT="${PREFIX} web (09_web.sh)"   # 배포를 다시 찾을 때 쓰므로 바꾸지 마세요
ORIGIN_ID="web-bucket"
ORIGIN_DOMAIN="${WEB_BUCKET}.s3.${AWS_REGION}.amazonaws.com"   # 버킷의 리전 주소 (웹사이트 주소 아님)

# CloudFront 관리형 정책 ID를 이름으로 찾습니다.
CACHE_POLICY_ID="$(aws cloudfront list-cache-policies --type managed \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='Managed-CachingOptimized'].CachePolicy.Id | [0]" \
  --output text)"
HEADERS_POLICY_ID="$(aws cloudfront list-response-headers-policies --type managed \
  --query "ResponseHeadersPolicyList.Items[?ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name=='Managed-SecurityHeadersPolicy'].ResponseHeadersPolicy.Id | [0]" \
  --output text)"
[ "$CACHE_POLICY_ID" != "None" ] || die "관리형 캐시 정책 CachingOptimized를 찾지 못했습니다."
[ "$HEADERS_POLICY_ID" != "None" ] || die "관리형 응답 헤더 정책 SecurityHeadersPolicy를 찾지 못했습니다."

find_distribution() {   # 설명(Comment)으로 배포 ID를 찾습니다. 없으면 빈 문자열.
  local id
  id="$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='${DIST_COMMENT}'].Id | [0]" --output text)"
  [ "$id" = "None" ] && id=""
  printf '%s' "$id"
}

# 버킷 정책에 더할 문장: 이 배포(OAC로 서명한 요청)만 객체를 읽을 수 있습니다. 사용법: oac_read_statement 배포ID
oac_read_statement() {
  cat <<EOF
{
      "Sid": "AllowCloudFrontReadOnly",
      "Effect": "Allow",
      "Principal": { "Service": "cloudfront.amazonaws.com" },
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${WEB_BUCKET}/*",
      "Condition": { "StringEquals": { "AWS:SourceArn": "arn:aws:cloudfront::${ACCOUNT_ID}:distribution/$1" } }
    }
EOF
}

log "웹 버킷 $WEB_BUCKET"
DIST_ID="$(find_distribution)"
# make_private_bucket은 버킷 정책을 통째로 다시 적습니다. 배포가 이미 있으면 읽기 허용 문장을 같이 넘겨서,
# 다시 실행하는 동안에도 CloudFront가 403을 받는 순간이 없게 합니다.
if [ -n "$DIST_ID" ]; then
  make_private_bucket "$WEB_BUCKET" off "$(oac_read_statement "$DIST_ID")"
  ok "  버킷 정책: 배포 $DIST_ID 만 읽기"
else
  make_private_bucket "$WEB_BUCKET" off   # 배포를 만든 뒤 아래에서 읽기 허용을 더합니다
fi

log "CloudFront OAC $OAC_NAME (S3, 요청마다 서명)"
# OAC는 태그를 달 수 없는 리소스라 태그가 없습니다.
OAC_ID="$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='${OAC_NAME}'].Id | [0]" --output text)"
if [ "$OAC_ID" != "None" ] && [ -n "$OAC_ID" ]; then
  skip "OAC $OAC_ID"
else
  OAC_ID="$(aws cloudfront create-origin-access-control --origin-access-control-config \
    "Name=${OAC_NAME},Description=${PREFIX} web bucket,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
    --query OriginAccessControl.Id --output text)"
  ok "OAC 생성 $OAC_ID"
fi

log "CloudFront 배포 (HTTPS로 넘김, 기본 문서 index.html, 보안 헤더)"
if [ -n "$DIST_ID" ]; then
  # 설정을 바꿔 다시 적용하지는 않습니다 (바꾸면 배포가 몇 분 동안 다시 퍼짐). 바꿀 때는 update-distribution을 씁니다.
  skip "배포 $DIST_ID"
else
  cat > "$TMP_DIR/web-distribution.json" <<EOF
{
  "DistributionConfig": {
    "CallerReference": "${PREFIX}-web-$(date +%Y%m%d%H%M%S)",
    "Comment": "${DIST_COMMENT}",
    "Enabled": true,
    "DefaultRootObject": "index.html",
    "Origins": {
      "Quantity": 1,
      "Items": [
        {
          "Id": "${ORIGIN_ID}",
          "DomainName": "${ORIGIN_DOMAIN}",
          "OriginPath": "",
          "S3OriginConfig": { "OriginAccessIdentity": "" },
          "OriginAccessControlId": "${OAC_ID}"
        }
      ]
    },
    "DefaultCacheBehavior": {
      "TargetOriginId": "${ORIGIN_ID}",
      "ViewerProtocolPolicy": "redirect-to-https",
      "AllowedMethods": {
        "Quantity": 2, "Items": [ "GET", "HEAD" ],
        "CachedMethods": { "Quantity": 2, "Items": [ "GET", "HEAD" ] }
      },
      "Compress": true,
      "CachePolicyId": "${CACHE_POLICY_ID}",
      "ResponseHeadersPolicyId": "${HEADERS_POLICY_ID}"
    },
    "PriceClass": "PriceClass_200",
    "HttpVersion": "http2",
    "IsIPV6Enabled": true,
    "ViewerCertificate": { "CloudFrontDefaultCertificate": true }
  },
  "Tags": { "Items": [ { "Key": "${TAG_KEY}", "Value": "${TAG_VALUE}" } ] }
}
EOF
  DIST_ID="$(aws cloudfront create-distribution-with-tags \
    --distribution-config-with-tags "file://${TMP_DIR}/web-distribution.json" \
    --query Distribution.Id --output text)"
  ok "배포 생성 $DIST_ID"
  put_bucket_policy "$WEB_BUCKET" "$(oac_read_statement "$DIST_ID")"
  ok "  버킷 정책: HTTPS 강제 + 배포 $DIST_ID 만 읽기"
fi
WEB_DOMAIN="$(aws cloudfront get-distribution --id "$DIST_ID" --query Distribution.DomainName --output text)"
WEB_URL="https://${WEB_DOMAIN}"
save_state WEB_BUCKET "$WEB_BUCKET"
save_state WEB_DISTRIBUTION_ID "$DIST_ID"
save_state WEB_DOMAIN "$WEB_DOMAIN"
save_state WEB_URL "$WEB_URL"

log "확인용 임시 index.html"
if exists aws s3api head-object --bucket "$WEB_BUCKET" --key index.html; then
  skip "index.html (진짜 화면일 수 있어서 덮어쓰지 않음)"
else
  mkdir -p "$TMP_DIR/web-placeholder"
  cat > "$TMP_DIR/web-placeholder/index.html" <<'EOF'
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>club-rag-hub</title>
</head>
<body>
<h1>club-rag-hub</h1>
<p>웹 호스팅 확인용 임시 페이지입니다. 웹 챗봇 화면을 배포하면 이 페이지는 바뀝니다.</p>
</body>
</html>
EOF
  # no-cache: 진짜 화면을 올렸을 때 CloudFront가 이 페이지를 오래 들고 있지 않게 합니다.
  aws s3 cp "$TMP_DIR/web-placeholder/index.html" "s3://${WEB_BUCKET}/index.html" \
    --content-type "text/html; charset=utf-8" --cache-control "no-cache" >/dev/null
  ok "index.html 올림"
fi

log "배포가 끝날 때까지 기다립니다 (처음에는 보통 5~10분)"
aws cloudfront wait distribution-deployed --id "$DIST_ID"
ok "배포 상태 Deployed"

# 본문은 $TMP_DIR에 받습니다 (MSYS_NO_PATHCONV=1이라 curl에 /dev/null을 주면 쓰기 오류가 남).
web_ok() { [ "$(curl -s -o "$TMP_DIR/web-check.html" -w '%{http_code}' "${WEB_URL}/")" = "200" ]; }
if retry 6 web_ok; then
  ok "${WEB_URL}/ 200"
else
  info "[주의] ${WEB_URL}/ 이 아직 200이 아닙니다. 몇 분 뒤 다시 열어 보세요."
fi

log "웹 주소: $WEB_URL"
info "다음: bash 07_cognito.sh (웹 앱 클라이언트의 돌아갈 주소에 이 주소를 넣음)"
