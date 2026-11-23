# Shared Etherscan source cache. Built and started by scripts/source_cache.sh.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CACHE_DIR=/cache \
    PORT=8080

COPY source_cache.py /usr/local/lib/rl-source-cache/source_cache.py

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).read()"]
ENTRYPOINT ["python3", "/usr/local/lib/rl-source-cache/source_cache.py"]
