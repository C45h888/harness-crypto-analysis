FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system marketflow && adduser --system --ingroup marketflow marketflow

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY market_service ./market_service
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# The canonical client modules may arrive from a source checkout with private
# mode bits. The runtime user must be able to import the complete package.
RUN chmod -R a+rX /app/market_service /app/alembic /app/alembic.ini

USER marketflow

# Each service specifies its own command via docker-compose. No default CMD
# here so the image is reusable for data-access, calculations, analysis,
# orchestrator, collector, and collator.
