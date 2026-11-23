#!/usr/bin/env bash
# 01. 월 예산 알림: 실제 요금이 80%를 넘거나, 이번 달 예상 요금이 100%를 넘으면 메일을 보냅니다.
# 예산(Budgets)은 전역 서비스라 us-east-1로 호출합니다. 태그는 달지 않았습니다.
source "$(dirname "$0")/lib.sh"

[ -n "${ALERT_EMAIL:-}" ] || die "config.local.env에 ALERT_EMAIL을 적으세요."
BUDGET_NAME="${PREFIX}-monthly"

log "월 예산 알림 ($BUDGET_NAME, ${BUDGET_USD} USD)"
if exists aws budgets describe-budget --region us-east-1 --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME"; then
  skip "예산 $BUDGET_NAME"
else
  cat > "$TMP_DIR/budget.json" <<EOF
{
  "BudgetName": "${BUDGET_NAME}",
  "BudgetLimit": { "Amount": "${BUDGET_USD}", "Unit": "USD" },
  "TimeUnit": "MONTHLY",
  "BudgetType": "COST"
}
EOF
  cat > "$TMP_DIR/budget-notify.json" <<EOF
[
  {
    "Notification": { "NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN", "Threshold": 80, "ThresholdType": "PERCENTAGE" },
    "Subscribers": [ { "SubscriptionType": "EMAIL", "Address": "${ALERT_EMAIL}" } ]
  },
  {
    "Notification": { "NotificationType": "FORECASTED", "ComparisonOperator": "GREATER_THAN", "Threshold": 100, "ThresholdType": "PERCENTAGE" },
    "Subscribers": [ { "SubscriptionType": "EMAIL", "Address": "${ALERT_EMAIL}" } ]
  }
]
EOF
  aws budgets create-budget --region us-east-1 --account-id "$ACCOUNT_ID" \
    --budget "file://${TMP_DIR}/budget.json" \
    --notifications-with-subscribers "file://${TMP_DIR}/budget-notify.json"
  ok "예산 $BUDGET_NAME 생성, 알림 메일 $ALERT_EMAIL"
fi
