FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system marketflow && adduser --system --ingroup marketflow marketflow

# nooa / nooa-cli come from PyPI wheels (pinned in requirements.txt), so no
# git or build toolchain is needed in the image.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY market_service ./market_service
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# Engine prompt-payload KBs (tool manifest, memory protocol, paper KB).
# These are read at inference time by engine.config and injected into the
# system prompt — without them the engine runs but ships empty KB blocks
# (null-discipline on KB load failure, see engine.kb.load_kb).
COPY docs/nooa-kb ./docs/nooa-kb

# The canonical client modules may arrive from a source checkout with private
# mode bits. The runtime user must be able to import the complete package
# AND the runtime must be able to read the KB docs.
RUN chmod -R a+rX /app/market_service /app/alembic /app/alembic.ini /app/docs

USER marketflow

# Each service specifies its own command via docker-compose. No default CMD
# here so the image is reusable for data-access, calculations, analysis,
# orchestrator, collector, and collator.
