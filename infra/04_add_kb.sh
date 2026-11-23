#!/usr/bin/env bash
# 04. 지식 베이스(KB) 하나를 만들어 챗봇에 연결합니다.
#     동아리를 하나 더 붙일 때는 이 스크립트를 이름만 바꿔 한 번 더 실행하면 됩니다.
#
# 만드는 것 (KB 하나당)
#   - 벡터 인덱스   kb-<kb_key>        (03에서 만든 벡터 버킷 안)
#   - IAM 역할      <PREFIX>-kb-<kb_key>  (자기 S3 폴더와 자기 인덱스만 접근)
#   - KB            <PREFIX>-<kb_key>
#   - 데이터 소스   <kb_key>-s3        (문서 버킷의 <s3_prefix> 폴더를 읽음)
#   - KB 목록 테이블에 한 줄 등록 (누가 검색할 수 있는지 포함)
#
# 사용법: bash 04_add_kb.sh <kb_key> <s3_prefix> <chunking> <allowed_groups>
#   chunking        NONE(파일 하나 = 청크 하나) 또는 FIXED_SIZE(300토큰, 20% 겹침)
#   allowed_groups  검색할 수 있는 Cognito 그룹, 쉼표로 구분. 모두 허용은 '*'
# 예) bash 04_add_kb.sh school   school/kb/    NONE       '*'
#     bash 04_add_kb.sh club-ana clubs/ana/kb/ FIXED_SIZE ana
source "$(dirname "$0")/lib.sh"

KB_KEY="${1:-}"
S3_PREFIX="${2:-}"
CHUNKING="${3:-}"
ALLOWED_GROUPS="${4:-}"

[[ "$KB_KEY" =~ ^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$ ]] || die "kb_key는 소문자, 숫자, 하이픈으로 3~30자: '$KB_KEY'"
[[ "$S3_PREFIX" =~ ^[a-z0-9][a-z0-9/_-]*/$ ]] || die "s3_prefix는 school/kb/ 처럼 /로 끝나야 합니다: '$S3_PREFIX'"
case "$CHUNKING" in
  NONE|FIXED_SIZE) ;;
  *) die "chunking은 NONE 또는 FIXED_SIZE: '$CHUNKING'" ;;
esac
[ -n "$ALLOWED_GROUPS" ] || die "allowed_groups를 적으세요 (모두 허용은 '*')"

INDEX_NAME="kb-${KB_KEY}"
INDEX_ARN="${VECTOR_BUCKET_ARN}/index/${INDEX_NAME}"
ROLE_NAME="${PREFIX}-kb-${KB_KEY}"
KB_NAME="${PREFIX}-${KB_KEY}"
DS_NAME="${KB_KEY}-s3"
STATE_KEY="$(printf '%s' "$KB_KEY" | tr 'a-z-' 'A-Z_')"

exists aws s3vectors get-vector-bucket --vector-bucket-name "$VECTOR_BUCKET" \
  || die "벡터 버킷 $VECTOR_BUCKET 이 없습니다. 03_vector_bucket.sh를 먼저 실행하세요."
exists aws dynamodb describe-table --table-name "$REGISTRY_TABLE" \
  || die "테이블 $REGISTRY_TABLE 이 없습니다. 02_storage.sh를 먼저 실행하세요."

# ---------------------------------------------------------------- 1. 벡터 인덱스
log "[$KB_KEY] 벡터 인덱스 $INDEX_NAME"
if exists aws s3vectors get-index --vector-bucket-name "$VECTOR_BUCKET" --index-name "$INDEX_NAME"; then
  skip "인덱스 $INDEX_NAME"
else
  # KB가 청크 본문을 AMAZON_BEDROCK_TEXT에 넣는데, 필터용 메타데이터는 크기 제한이 작아서
  # 이 두 키는 필터에 쓰지 않는 메타데이터로 지정해야 적재 오류가 나지 않습니다.
  aws s3vectors create-index \
    --vector-bucket-name "$VECTOR_BUCKET" \
    --index-name "$INDEX_NAME" \
    --data-type float32 \
    --dimension "$EMBED_DIM" \
    --distance-metric "$DISTANCE_METRIC" \
    --metadata-configuration '{"nonFilterableMetadataKeys":["AMAZON_BEDROCK_TEXT","AMAZON_BEDROCK_METADATA"]}' \
    --tags "${TAG_MAP},KbKey=${KB_KEY}" >/dev/null
  ok "인덱스 $INDEX_NAME 생성 (${EMBED_DIM}차원, $DISTANCE_METRIC)"
fi

# ---------------------------------------------------------------- 2. KB 전용 IAM 역할
log "[$KB_KEY] IAM 역할 $ROLE_NAME"
cat > "$TMP_DIR/kb-trust-${KB_KEY}.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "bedrock.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "aws:SourceAccount": "${ACCOUNT_ID}" },
        "ArnLike": { "aws:SourceArn": "arn:aws:bedrock:${AWS_REGION}:${ACCOUNT_ID}:knowledge-base/*" }
      }
    }
  ]
}
EOF
cat > "$TMP_DIR/kb-policy-${KB_KEY}.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListModels",
      "Effect": "Allow",
      "Action": [ "bedrock:ListFoundationModels", "bedrock:ListCustomModels" ],
      "Resource": "*"
    },
    {
      "Sid": "InvokeEmbeddingModel",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": "${EMBED_MODEL_ARN}"
    },
    {
      "Sid": "ListDocsBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::${DOCS_BUCKET}",
      "Condition": { "StringEquals": { "aws:ResourceAccount": "${ACCOUNT_ID}" } }
    },
    {
      "Sid": "ReadOwnPrefixOnly",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${DOCS_BUCKET}/${S3_PREFIX}*",
      "Condition": { "StringEquals": { "aws:ResourceAccount": "${ACCOUNT_ID}" } }
    },
    {
      "Sid": "OwnVectorIndexOnly",
      "Effect": "Allow",
      "Action": [
        "s3vectors:PutVectors",
        "s3vectors:GetVectors",
        "s3vectors:DeleteVectors",
        "s3vectors:QueryVectors",
        "s3vectors:GetIndex"
      ],
      "Resource": "${INDEX_ARN}"
    }
  ]
}
EOF
new_role=0
if exists aws iam get-role --role-name "$ROLE_NAME"; then
  skip "역할 $ROLE_NAME"
else
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://${TMP_DIR}/kb-trust-${KB_KEY}.json" \
    --description "Bedrock KB service role for ${KB_KEY}" \
    --tags "$TAG_LIST" "Key=KbKey,Value=${KB_KEY}" >/dev/null
  new_role=1
  ok "역할 $ROLE_NAME 생성"
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name kb-access \
  --policy-document "file://${TMP_DIR}/kb-policy-${KB_KEY}.json"
ok "  권한: 임베딩 모델 호출, s3://${DOCS_BUCKET}/${S3_PREFIX} 읽기, 인덱스 $INDEX_NAME 읽기/쓰기"
ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
if [ "$new_role" = 1 ]; then
  info "새 역할이 적용될 때까지 15초 기다립니다"
  sleep 15
fi

# ---------------------------------------------------------------- 3. 지식 베이스
log "[$KB_KEY] 지식 베이스 $KB_NAME"
cat > "$TMP_DIR/kb-config-${KB_KEY}.json" <<EOF
{
  "type": "VECTOR",
  "vectorKnowledgeBaseConfiguration": {
    "embeddingModelArn": "${EMBED_MODEL_ARN}",
    "embeddingModelConfiguration": {
      "bedrockEmbeddingModelConfiguration": { "dimensions": ${EMBED_DIM}, "embeddingDataType": "FLOAT32" }
    }
  }
}
EOF
cat > "$TMP_DIR/kb-storage-${KB_KEY}.json" <<EOF
{
  "type": "S3_VECTORS",
  "s3VectorsConfiguration": { "indexArn": "${INDEX_ARN}" }
}
EOF
KB_ID="$(aws bedrock-agent list-knowledge-bases \
  --query "knowledgeBaseSummaries[?name=='${KB_NAME}'].knowledgeBaseId | [0]" --output text)"
if [ -n "$KB_ID" ] && [ "$KB_ID" != "None" ]; then
  skip "KB $KB_NAME ($KB_ID)"
else
  # 방금 만든 역할은 바로 인식되지 않을 때가 있어서 몇 번 다시 시도합니다.
  KB_ID="$(retry 4 aws bedrock-agent create-knowledge-base \
    --name "$KB_NAME" \
    --description "club-rag-hub knowledge base: ${KB_KEY}" \
    --role-arn "$ROLE_ARN" \
    --knowledge-base-configuration "file://${TMP_DIR}/kb-config-${KB_KEY}.json" \
    --storage-configuration "file://${TMP_DIR}/kb-storage-${KB_KEY}.json" \
    --tags "${TAG_MAP},KbKey=${KB_KEY}" \
    --query knowledgeBase.knowledgeBaseId --output text)" \
    || die "KB를 만들지 못했습니다. 위 오류 메시지를 확인하세요."
  ok "KB $KB_NAME 생성 ($KB_ID)"
fi

info "KB가 ACTIVE가 될 때까지 기다립니다"
status=""
for _ in $(seq 1 36); do
  status="$(aws bedrock-agent get-knowledge-base --knowledge-base-id "$KB_ID" \
    --query knowledgeBase.status --output text)"
  case "$status" in
    ACTIVE) break ;;
    FAILED) die "KB 상태가 FAILED입니다. 콘솔에서 $KB_NAME 의 실패 이유를 확인하세요." ;;
  esac
  sleep 5
done
[ "$status" = "ACTIVE" ] || die "3분이 지나도 ACTIVE가 아닙니다 (현재 $status)."
ok "KB 상태 ACTIVE"

# ---------------------------------------------------------------- 4. 데이터 소스
log "[$KB_KEY] 데이터 소스 $DS_NAME (s3://${DOCS_BUCKET}/${S3_PREFIX})"
cat > "$TMP_DIR/ds-config-${KB_KEY}.json" <<EOF
{
  "type": "S3",
  "s3Configuration": {
    "bucketArn": "arn:aws:s3:::${DOCS_BUCKET}",
    "inclusionPrefixes": [ "${S3_PREFIX}" ]
  }
}
EOF
if [ "$CHUNKING" = "NONE" ]; then
  echo '{ "chunkingConfiguration": { "chunkingStrategy": "NONE" } }' > "$TMP_DIR/ds-chunk-${KB_KEY}.json"
else
  echo '{ "chunkingConfiguration": { "chunkingStrategy": "FIXED_SIZE", "fixedSizeChunkingConfiguration": { "maxTokens": 300, "overlapPercentage": 20 } } }' \
    > "$TMP_DIR/ds-chunk-${KB_KEY}.json"
fi
DS_ID="$(aws bedrock-agent list-data-sources --knowledge-base-id "$KB_ID" \
  --query "dataSourceSummaries[?name=='${DS_NAME}'].dataSourceId | [0]" --output text)"
if [ -n "$DS_ID" ] && [ "$DS_ID" != "None" ]; then
  skip "데이터 소스 $DS_NAME ($DS_ID)"
else
  DS_ID="$(aws bedrock-agent create-data-source \
    --knowledge-base-id "$KB_ID" \
    --name "$DS_NAME" \
    --data-source-configuration "file://${TMP_DIR}/ds-config-${KB_KEY}.json" \
    --vector-ingestion-configuration "file://${TMP_DIR}/ds-chunk-${KB_KEY}.json" \
    --query dataSource.dataSourceId --output text)"
  ok "데이터 소스 $DS_NAME 생성 ($DS_ID, 청킹 $CHUNKING)"
fi

# ---------------------------------------------------------------- 5. KB 목록에 등록
log "[$KB_KEY] KB 목록 테이블에 등록"
groups_json=""
IFS=',' read -r -a group_arr <<< "$ALLOWED_GROUPS"
for g in "${group_arr[@]}"; do
  g="$(printf '%s' "$g" | tr -d '[:space:]')"
  [ -n "$g" ] && groups_json="${groups_json}\"${g}\","
done
groups_json="[${groups_json%,}]"
cat > "$TMP_DIR/registry-${KB_KEY}.json" <<EOF
{
  "kb_key":         { "S": "${KB_KEY}" },
  "kb_id":          { "S": "${KB_ID}" },
  "data_source_id": { "S": "${DS_ID}" },
  "index_name":     { "S": "${INDEX_NAME}" },
  "s3_prefix":      { "S": "${S3_PREFIX}" },
  "chunking":       { "S": "${CHUNKING}" },
  "allowed_groups": { "SS": ${groups_json} },
  "enabled":        { "BOOL": true },
  "updated_at":     { "S": "$(date -u +%Y-%m-%dT%H:%M:%SZ)" }
}
EOF
aws dynamodb put-item --table-name "$REGISTRY_TABLE" --item "file://${TMP_DIR}/registry-${KB_KEY}.json"
ok "등록: $KB_KEY -> $KB_ID (검색 허용 그룹 $groups_json)"

save_state "KB_ID_${STATE_KEY}" "$KB_ID"
save_state "DS_ID_${STATE_KEY}" "$DS_ID"
info "문서는 s3://${DOCS_BUCKET}/${S3_PREFIX} 에 올리면 이 KB가 읽습니다."
