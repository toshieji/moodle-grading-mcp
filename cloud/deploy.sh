#!/usr/bin/env bash
# 上級ウェブ解析士 採点ドラフト日次バッチを Cloud Run Jobs + Cloud Scheduler にデプロイする（Mac非依存）。
# 前提: gcloud にログイン済み・対象プロジェクトの権限あり・.env に MOODLE_TOKEN がある。
#
# 使い方:
#   MOODLE_TOKEN=xxxx ANTHROPIC_API_KEY=sk-ant-xxxx ./deploy.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT="${PROJECT:-databeatwaca210204}"
REGION="${REGION:-asia-northeast1}"
JOB_NAME="${JOB_NAME:-moodle-grading-daily}"
SA_NAME="${SA_NAME:-moodle-grading-job}"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHEDULER_NAME="${SCHEDULER_NAME:-moodle-grading-daily-trigger}"
SCHEDULE="${SCHEDULE:-0 6 * * *}"   # 毎朝6:00 JST
TIME_ZONE="${TIME_ZONE:-Asia/Tokyo}"

: "${MOODLE_TOKEN:?MOODLE_TOKEN を環境変数で渡してください（採点権限ユーザのMoodle Web Servicesトークン。TOKEN-setup.md参照）}"
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY を環境変数で渡してください（console.anthropic.com で発行）}"
MOODLE_URL="${MOODLE_URL:-https://moodle.waca.associates}"   # Moodle本体のURL（.env.example準拠）。違う場合は MOODLE_URL=... で上書き
GRADE_COURSE_IDS="${GRADE_COURSE_IDS:-798,796}"
MOODLE_WRITE_COURSE_ALLOWLIST="${MOODLE_WRITE_COURSE_ALLOWLIST:-798,796}"
MOODLE_ALLOW_WRITE="${MOODLE_ALLOW_WRITE:-1}"
RUBRIC_SHEET_ID="${RUBRIC_SHEET_ID:-1bpQvKMMxQtjmn3ruhuVR8UNQAhQuWnMQMIUm21E6zv0}"
RUBRIC_SHEET_GID="${RUBRIC_SHEET_GID:-1694745353}"
GRADING_MODEL="${GRADING_MODEL:-claude-haiku-4-5-20251001}"

echo "== 1. サービスアカウント作成（既存ならスキップ） =="
gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
    --display-name="Moodle grading daily job"

echo "!! 手動作業が必要です:"
echo "   採点基準スプレッドシートを ${SA_EMAIL} と「閲覧者」共有してください（Sheets APIで読むため）。"
echo "   URL: https://docs.google.com/spreadsheets/d/${RUBRIC_SHEET_ID}/edit"
read -r -p "共有済みですか？ [y/N] " ans
[[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "共有してから再実行してください。"; exit 1; }

echo "== 2. Moodle トークンを Secret Manager に保存 =="
if gcloud secrets describe moodle-grading-token --project "$PROJECT" >/dev/null 2>&1; then
  printf '%s' "$MOODLE_TOKEN" | gcloud secrets versions add moodle-grading-token --project "$PROJECT" --data-file=-
else
  printf '%s' "$MOODLE_TOKEN" | gcloud secrets create moodle-grading-token --project "$PROJECT" --data-file=- --replication-policy=automatic
fi
gcloud secrets add-iam-policy-binding moodle-grading-token --project "$PROJECT" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor" >/dev/null

echo "== 3. Anthropic APIキーを Secret Manager に保存 =="
if gcloud secrets describe anthropic-api-key --project "$PROJECT" >/dev/null 2>&1; then
  printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets versions add anthropic-api-key --project "$PROJECT" --data-file=-
else
  printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets create anthropic-api-key --project "$PROJECT" --data-file=- --replication-policy=automatic
fi
gcloud secrets add-iam-policy-binding anthropic-api-key --project "$PROJECT" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor" >/dev/null

echo "== 4. イメージビルド（Cloud Build） =="
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/moodle-grading/grading-job:latest"
gcloud artifacts repositories describe moodle-grading --project "$PROJECT" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create moodle-grading --project "$PROJECT" --location "$REGION" --repository-format=docker
# extract.py はリポジトリ直下（MCP サーバと共用）にあるため、cloud/ と合わせた一時ディレクトリでビルドする。
BUILD_DIR="$(mktemp -d)"
cp "$HERE"/Dockerfile "$HERE"/requirements.txt "$HERE"/grading_job.py "$HERE"/ai-usage-log-rubric.md "$BUILD_DIR"/
cp "$HERE"/../extract.py "$BUILD_DIR"/
gcloud builds submit "$BUILD_DIR" --project "$PROJECT" --tag "$IMAGE"
rm -rf "$BUILD_DIR"

echo "== 5. Cloud Run Job 作成/更新 =="
# 値にカンマを含む変数（GRADE_COURSE_IDS等）があるため --set-env-vars の単純カンマ区切りは使えない。
# env-vars-file（YAML）経由で渡す。
ENV_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT
cat > "$ENV_FILE" <<EOF
MOODLE_URL: "${MOODLE_URL}"
GRADE_COURSE_IDS: "${GRADE_COURSE_IDS}"
MOODLE_WRITE_COURSE_ALLOWLIST: "${MOODLE_WRITE_COURSE_ALLOWLIST}"
MOODLE_ALLOW_WRITE: "${MOODLE_ALLOW_WRITE}"
RUBRIC_SHEET_ID: "${RUBRIC_SHEET_ID}"
RUBRIC_SHEET_GID: "${RUBRIC_SHEET_GID}"
GRADING_MODEL: "${GRADING_MODEL}"
EOF

gcloud run jobs deploy "$JOB_NAME" \
  --project "$PROJECT" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$SA_EMAIL" \
  --env-vars-file "$ENV_FILE" \
  --set-secrets "MOODLE_TOKEN=moodle-grading-token:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest" \
  --max-retries 1 --task-timeout 30m --cpu 1 --memory 512Mi

echo "== 6. Scheduler 用サービスアカウントに Cloud Run 起動権限を付与 =="
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/run.invoker" --condition=None >/dev/null

JOB_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB_NAME}:run"

echo "== 7. Cloud Scheduler 登録（毎朝 ${SCHEDULE} ${TIME_ZONE}） =="
if gcloud scheduler jobs describe "$SCHEDULER_NAME" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SCHEDULER_NAME" --project "$PROJECT" --location "$REGION" \
    --schedule="$SCHEDULE" --time-zone="$TIME_ZONE" --uri="$JOB_URI" --http-method=POST \
    --oauth-service-account-email="$SA_EMAIL"
else
  gcloud scheduler jobs create http "$SCHEDULER_NAME" --project "$PROJECT" --location "$REGION" \
    --schedule="$SCHEDULE" --time-zone="$TIME_ZONE" --uri="$JOB_URI" --http-method=POST \
    --oauth-service-account-email="$SA_EMAIL"
fi

echo "完了。動作確認: gcloud run jobs execute ${JOB_NAME} --project ${PROJECT} --region ${REGION}"
