FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
RUN groupadd --gid 10001 puaa \
    && useradd --uid 10001 --gid puaa --no-create-home --shell /usr/sbin/nologin puaa \
    && mkdir -p /app/data \
    && chown puaa:puaa /app/data

USER puaa:puaa
EXPOSE 8000
CMD ["python", "-m", "app"]
