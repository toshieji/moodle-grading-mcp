FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grading_job.py .
COPY ai-usage-log-rubric.md .

ENTRYPOINT ["python", "grading_job.py"]
