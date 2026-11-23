#!/usr/bin/env bash
# 05. GitHub Actions 배포 권한: OIDC 공급자 + 배포 역할
#     AWS 키를 GitHub에 넣지 않고, main 브랜치의 워크플로만 이 역할을 잠깐 빌려 SAM 앱을 배포합니다.
source "$(dirname "$0")/lib.sh"

OIDC_HOST="token.actions.githubusercontent.com"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_HOST}"
# OIDC 토큰의 sub 앞부분은 저장소 설정에 따라 다릅니다. 새 저장소는 소유자·저장소 ID가 붙은 형식
# (repo:소유자@ID/저장소@ID, immutable subject)을 써서, 이름을 바꾸거나 같은 이름으로 다시 만든 저장소는 역할을 못 받습니다.
# 그래서 GitHub에 직접 물어봅니다 (11/23 첫 Actions 실행이 옛 형식 때문에 거부되어 바꿈).
SUB_PREFIX="$(gh api "repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/oidc/customization/sub" --jq .sub_claim_prefix 2>/dev/null || true)"
if [ -z "$SUB_PREFIX" ]; then
  SUB_PREFIX="repo:${GITHUB_OWNER}/${GITHUB_REPO}"
  info "[주의] gh로 sub 형식을 확인하지 못해 옛 형식($SUB_PREFIX)을 씁니다. Actions가 거부되면 gh 로그인 후 다시 실행하세요."
fi
GITHUB_SUB="${SUB_PREFIX}:ref:refs/heads/main"

log "OIDC 공급자 $OIDC_HOST"
if exists aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN"; then
  skip "OIDC 공급자"
else
  # 지문(thumbprint)은 GitHub가 안내한 값입니다. CLI 버전에 따라 빈 목록을 거부할 때가 있어 넣어 둡니다.
  aws iam create-open-id-connect-provider \
    --url "https://${OIDC_HOST}" \
    --client-id-list sts.amazonaws.com \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
    --tags "$TAG_LIST" >/dev/null
  ok "OIDC 공급자 생성"
fi

log "배포 역할 $DEPLOY_ROLE (허용: ${GITHUB_SUB})"
cat > "$TMP_DIR/deploy-trust.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Federated": "${OIDC_ARN}" },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_HOST}:aud": "sts.amazonaws.com",
          "${OIDC_HOST}:sub": "${GITHUB_SUB}"
        }
      }
    }
  ]
}
EOF
# SAM 앱 스택(${APP_STACK})과 그 스택이 만드는 Lambda, 역할, HTTP API, 로그 그룹만 다룰 수 있게 범위를 좁혔습니다.
# Lambda의 Get*Config와 logs의 Describe*/GetDataProtectionPolicy는 CloudFormation이 리소스를 만들고 바꾼 뒤
# 상태를 다시 읽을 때 부르는 읽기 권한입니다 (리소스 타입 스키마의 handlers 권한, 11/23 확인).
# 11/23 첫 배포의 CloudTrail 기록에서 CloudFormation이 실제로 부른 API와 대조했습니다.
# API Gateway는 HTTP API를 만들 때 자기 서비스 연결 역할을 만들려고 시도하므로, 그 역할만 만들 수 있게 열어 둡니다.
cat > "$TMP_DIR/deploy-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AppStackOnly",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:DeleteStack",
        "cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet",
        "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet",
        "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResource", "cloudformation:DescribeStackResources",
        "cloudformation:ListStackResources", "cloudformation:GetTemplate", "cloudformation:TagResource"
      ],
      "Resource": "arn:aws:cloudformation:${AWS_REGION}:${ACCOUNT_ID}:stack/${APP_STACK}/*"
    },
    {
      "Sid": "SamTransform",
      "Effect": "Allow",
      "Action": "cloudformation:CreateChangeSet",
      "Resource": "arn:aws:cloudformation:${AWS_REGION}:aws:transform/Serverless-2016-10-31"
    },
    {
      "Sid": "CfnReadOnly",
      "Effect": "Allow",
      "Action": [ "cloudformation:ValidateTemplate", "cloudformation:GetTemplateSummary", "cloudformation:ListStacks" ],
      "Resource": "*"
    },
    {
      "Sid": "ArtifactObjects",
      "Effect": "Allow",
      "Action": [ "s3:PutObject", "s3:GetObject" ],
      "Resource": "arn:aws:s3:::${ARTIFACT_BUCKET}/*"
    },
    {
      "Sid": "ArtifactBucket",
      "Effect": "Allow",
      "Action": [ "s3:ListBucket", "s3:GetBucketLocation" ],
      "Resource": "arn:aws:s3:::${ARTIFACT_BUCKET}"
    },
    {
      "Sid": "AppFunctions",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction",
        "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
        "lambda:AddPermission", "lambda:RemovePermission", "lambda:GetPolicy",
        "lambda:TagResource", "lambda:UntagResource", "lambda:ListTags",
        "lambda:PublishVersion", "lambda:ListVersionsByFunction",
        "lambda:GetFunctionCodeSigningConfig", "lambda:GetFunctionRecursionConfig",
        "lambda:GetFunctionScalingConfig", "lambda:GetRuntimeManagementConfig"
      ],
      "Resource": "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${APP_STACK}-*"
    },
    {
      "Sid": "AppRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole",
        "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
        "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:UpdateAssumeRolePolicy"
      ],
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/${APP_STACK}-*"
    },
    {
      "Sid": "AttachLambdaBasicOnly",
      "Effect": "Allow",
      "Action": [ "iam:AttachRolePolicy", "iam:DetachRolePolicy" ],
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/${APP_STACK}-*",
      "Condition": { "ArnEquals": { "iam:PolicyARN": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" } }
    },
    {
      "Sid": "PassRoleToLambdaOnly",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/${APP_STACK}-*",
      "Condition": { "StringEquals": { "iam:PassedToService": "lambda.amazonaws.com" } }
    },
    {
      "Sid": "HttpApi",
      "Effect": "Allow",
      "Action": [
        "apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE",
        "apigateway:TagResource", "apigateway:UntagResource"
      ],
      "Resource": [
        "arn:aws:apigateway:${AWS_REGION}::/apis",
        "arn:aws:apigateway:${AWS_REGION}::/apis/*",
        "arn:aws:apigateway:${AWS_REGION}::/tags/*"
      ]
    },
    {
      "Sid": "AppLogGroups",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
        "logs:TagResource", "logs:UntagResource", "logs:TagLogGroup", "logs:ListTagsForResource",
        "logs:DescribeIndexPolicies", "logs:GetDataProtectionPolicy"
      ],
      "Resource": [
        "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${APP_STACK}-*",
        "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${APP_STACK}-*:*"
      ]
    },
    {
      "Sid": "AccountReadOnly",
      "Effect": "Allow",
      "Action": [ "lambda:GetAccountSettings", "iam:GetAccountSummary" ],
      "Resource": "*"
    },
    {
      "Sid": "ApiGatewayServiceLinkedRoleOnly",
      "Effect": "Allow",
      "Action": "iam:CreateServiceLinkedRole",
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/aws-service-role/ops.apigateway.amazonaws.com/*",
      "Condition": { "StringEquals": { "iam:AWSServiceName": "ops.apigateway.amazonaws.com" } }
    },
    {
      "Sid": "DescribeLogGroups",
      "Effect": "Allow",
      "Action": [ "logs:DescribeLogGroups", "logs:DescribeResourcePolicies", "logs:DescribeIndexPolicies" ],
      "Resource": "*"
    }
  ]
}
EOF
if exists aws iam get-role --role-name "$DEPLOY_ROLE"; then
  skip "역할 $DEPLOY_ROLE (신뢰 정책은 다시 적용합니다)"
  aws iam update-assume-role-policy --role-name "$DEPLOY_ROLE" \
    --policy-document "file://${TMP_DIR}/deploy-trust.json"
else
  aws iam create-role --role-name "$DEPLOY_ROLE" \
    --assume-role-policy-document "file://${TMP_DIR}/deploy-trust.json" \
    --description "GitHub Actions deploy role for ${GITHUB_OWNER}/${GITHUB_REPO}" \
    --max-session-duration 3600 \
    --tags "$TAG_LIST" >/dev/null
  ok "역할 $DEPLOY_ROLE 생성"
fi
aws iam put-role-policy --role-name "$DEPLOY_ROLE" --policy-name sam-app-deploy \
  --policy-document "file://${TMP_DIR}/deploy-policy.json"
ok "  권한: ${APP_STACK} 스택 배포에 필요한 것만"

DEPLOY_ROLE_ARN="$(aws iam get-role --role-name "$DEPLOY_ROLE" --query Role.Arn --output text)"
save_state DEPLOY_ROLE_ARN "$DEPLOY_ROLE_ARN"

log "다음: bash 06_github_actions.sh 가 이 ARN을 저장소 Secrets의 AWS_ROLE_ARN으로 등록합니다"
