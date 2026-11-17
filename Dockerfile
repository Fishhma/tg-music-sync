FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install telethon==1.36.0 mutagen==1.47.0 tzdata

WORKDIR /app
COPY sync.py tagfix.py ./

CMD ["python", "sync.py"]
