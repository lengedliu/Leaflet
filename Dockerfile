FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 CONFIG_DIR=/config
WORKDIR /app
COPY --chown=10001:10001 index.html manifest.webmanifest sw.js server.py icon.svg cover-placeholder.svg ./
RUN mkdir -p /config && chown 10001:10001 /config
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)" || exit 1
CMD ["python", "server.py"]
