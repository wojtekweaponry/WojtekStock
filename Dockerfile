# Wojtek Stock. Standard library only, so the image is just Python and two files.
FROM python:3.12-slim

# tzdata: the shop's time zone for the nightly sync and backup schedule
RUN pip install --no-cache-dir tzdata \
 && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin stock \
 && mkdir -p /app /data && chown stock:stock /data
WORKDIR /app
COPY server.py index.html /app/

ENV STOCK_DB=/data/stock.db \
    STOCK_HOST=0.0.0.0 \
    STOCK_PORT=8090 \
    PYTHONUNBUFFERED=1
USER stock
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4)"
CMD ["python3", "/app/server.py"]
