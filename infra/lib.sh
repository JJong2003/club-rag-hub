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
