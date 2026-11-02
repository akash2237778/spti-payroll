FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAYROLL_DB=/data/payroll.db \
    PORT=8000 \
    TZ=Asia/Kolkata

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir flask pyzk openpyxl waitress

COPY run.py .
COPY payroll ./payroll

# Run unprivileged; /data is the persistent volume (database, secret key, backups).
RUN useradd --uid 568 --create-home payroll && mkdir -p /data && chown payroll:payroll /data
USER 568
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'), timeout=4)"

CMD ["python", "run.py"]
