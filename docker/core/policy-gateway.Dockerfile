# Per-trial policy gateway sidecar (pier-policy), versioned separately from task
# images so gateway changes never require rebuilding them.
# Build: docker build -f docker/core/policy-gateway.Dockerfile -t rl-policy-gateway:v2 docker/core
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY policy_gateway.py /usr/local/lib/rl-task/policy_gateway.py

USER 10001:10001
ENTRYPOINT ["python3", "/usr/local/lib/rl-task/policy_gateway.py"]
