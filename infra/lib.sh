#!/usr/bin/env bash
# 모든 스크립트가 맨 처음에 불러오는 공통 설정과 함수입니다. 직접 실행하지 않습니다.
# (아래 변수들은 이 파일을 불러오는 스크립트에서 쓰기 때문에 "안 쓰는 변수" 경고를 끕니다.)
# shellcheck disable=SC2034
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INFRA_DIR"

# Git Bash(Windows)에서 /로 시작하는 인자가 Windows 경로로 바뀌는 것을 막습니다.
export MSYS_NO_PATHCONV=1
# aws 출력이 페이저에 걸려 멈추지 않게 합니다.
export AWS_PAGER=""
# Windows에서 aws가 file:// JSON을 시스템 인코딩(CP949)으로 읽어 한글이 깨지는 것을 막습니다.
export AWS_CLI_FILE_ENCODING="UTF-8"

[ -f config.env ] || { echo "  [오류] config.env 파일이 없습니다." >&2; exit 1; }
# shellcheck source=/dev/null
source ./config.env
# 메일 주소처럼 저장소에 올리지 않을 값은 config.local.env에 둡니다 (.gitignore 처리됨).
if [ -f config.local.env ]; then
  # shellcheck source=/dev/null
  source ./config.local.env
fi

export AWS_PROFILE="$AWS_PROFILE_NAME"
export AWS_REGION
export AWS_DEFAULT_REGION="$AWS_REGION"

# aws에 file://로 넘기는 JSON은 이 폴더에 만들고, 항상 상대 경로로 넘깁니다.
TMP_DIR=".state/tmp"
STATE_FILE=".state/outputs.env"
mkdir -p "$TMP_DIR"
touch "$STATE_FILE"

log()  { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
ok()   { printf '  [완료] %s\n' "$*" >&2; }
skip() { printf '  [건너뜀] %s - 이미 있습니다\n' "$*" >&2; }
info() { printf '  %s\n' "$*" >&2; }
die()  { printf '  [오류] %s\n' "$*" >&2; exit 1; }

# 명령이 성공하면 참입니다. 리소스가 이미 있는지 확인할 때 씁니다.
exists() { "$@" >/dev/null 2>&1; }

# 실패하면 10초 간격으로 다시 시도합니다. 사용법: retry 횟수 명령...
retry() {
  local max="$1" n=1
  shift
  until "$@"; do
    [ "$n" -ge "$max" ] && return 1
    info "다시 시도합니다 ($n/$max), 10초 후"
    n=$((n + 1))
    sleep 10
  done
}

# 만든 리소스의 ID를 .state/outputs.env에 적습니다. 사용법: save_state 이름 값
save_state() {
  local key="$1" val="$2"
  { grep -v "^${key}=" "$STATE_FILE" || true; printf '%s="%s"\n' "$key" "$val"; } > "$STATE_FILE.new"
  mv "$STATE_FILE.new" "$STATE_FILE"
}
# shellcheck source=/dev/null
source "$STATE_FILE"

# 설정값 확인
[[ "$PREFIX" =~ ^[a-z0-9][a-z0-9-]{1,18}[a-z0-9]$ ]] \
  || die "PREFIX는 소문자, 숫자, 하이픈으로 3~20자여야 합니다: $PREFIX"
if [ -z "${TAG_KEY:-}" ] || [ -z "${TAG_VALUE:-}" ]; then die "TAG_KEY, TAG_VALUE를 채우세요."; fi

# 계정 ID는 처음 한 번만 조회해서 적어 둡니다.
if [ -z "${ACCOUNT_ID:-}" ]; then
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
    || die "AWS 자격 증명을 읽지 못했습니다. 먼저 'aws configure --profile $AWS_PROFILE'을 하세요."
  save_state ACCOUNT_ID "$ACCOUNT_ID"
fi

# 리소스 이름 (모두 PREFIX에서 만들어집니다)
DOCS_BUCKET="${PREFIX}-docs-${ACCOUNT_ID}"
ARTIFACT_BUCKET="${PREFIX}-artifacts-${ACCOUNT_ID}"
WEB_BUCKET="${PREFIX}-web-${ACCOUNT_ID}"
VECTOR_BUCKET="${PREFIX}-vectors-${ACCOUNT_ID}"
VECTOR_BUCKET_ARN="arn:aws:s3vectors:${AWS_REGION}:${ACCOUNT_ID}:bucket/${VECTOR_BUCKET}"
RULE_TABLE="${PREFIX}-rule-sync-state"
REGISTRY_TABLE="${PREFIX}-kb-registry"
APP_STACK="${PREFIX}-app"
DEPLOY_ROLE="${PREFIX}-github-deploy"

# 모든 KB가 같은 임베딩 설정을 써야 KB끼리 검색 점수를 비교할 수 있습니다. 바꾸지 마세요.
EMBED_MODEL_ID="amazon.titan-embed-text-v2:0"
EMBED_MODEL_ARN="arn:aws:bedrock:${AWS_REGION}::foundation-model/${EMBED_MODEL_ID}"
EMBED_DIM=1024
DISTANCE_METRIC="cosine"

# 태그 형식이 서비스마다 달라서 두 가지로 준비합니다.
TAG_LIST="Key=${TAG_KEY},Value=${TAG_VALUE}"   # IAM, DynamoDB 형식
TAG_MAP="${TAG_KEY}=${TAG_VALUE}"              # S3 Vectors, Bedrock 형식

# 버킷 정책을 통째로 새로 적습니다. HTTPS가 아닌 요청은 모두 거부하고, 추가 문장이 있으면 뒤에 붙입니다.
# 사용법: put_bucket_policy 버킷이름 [추가 문장(JSON 객체. 여러 개면 쉼표로 이어서)]
put_bucket_policy() {
  local b="$1" extra="${2:-}" more=""
  if [ -n "$extra" ]; then
    more=",
    ${extra}"
  fi
  cat > "$TMP_DIR/tls-${b}.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [ "arn:aws:s3:::${b}", "arn:aws:s3:::${b}/*" ],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }${more}
  ]
}
EOF
  aws s3api put-bucket-policy --bucket "$b" --policy "file://${TMP_DIR}/tls-${b}.json"
}

# 퍼블릭 차단, 기본 암호화, HTTPS 강제, 태그를 건 비공개 버킷을 만듭니다 (02, 09가 씀).
# 버킷 정책은 통째로 바뀌므로, 정책에 더 넣을 문장이 있으면 세 번째 인자로 넘깁니다.
# 사용법: make_private_bucket 버킷이름 on|off(버전 관리) [버킷 정책에 더할 문장]
make_private_bucket() {
  local b="$1" versioning="$2" extra="${3:-}"
  if exists aws s3api head-bucket --bucket "$b"; then
    skip "S3 버킷 $b"
  else
    aws s3api create-bucket --bucket "$b" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
    aws s3api wait bucket-exists --bucket "$b"
    ok "S3 버킷 $b 생성"
  fi

  aws s3api put-public-access-block --bucket "$b" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "$b" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  if [ "$versioning" = "on" ]; then
    aws s3api put-bucket-versioning --bucket "$b" --versioning-configuration Status=Enabled
  fi

  put_bucket_policy "$b" "$extra"
  aws s3api put-bucket-tagging --bucket "$b" --tagging "TagSet=[{Key=${TAG_KEY},Value=${TAG_VALUE}}]"
  ok "  퍼블릭 차단, 암호화, HTTPS 강제, 태그 적용 (버전 관리: $versioning)"
}
