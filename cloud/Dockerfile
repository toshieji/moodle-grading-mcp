FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grading_job.py .
# 提出ファイルの本文・画像抽出（リポジトリ直下の extract.py。deploy.sh がビルド前に同梱する）
COPY extract.py .
COPY rubric.py .

ENTRYPOINT ["python", "grading_job.py"]
