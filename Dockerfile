FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system marketflow && adduser --system --ingroup marketflow marketflow

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY binance.py ./
COPY market_service ./market_service

USER marketflow

CMD ["python", "-m", "market_service.collector"]
