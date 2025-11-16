# syntax=docker/dockerfile:1.6

FROM python:3.11-slim AS requirements
ENV PYTHONUNBUFFERED=1
WORKDIR /tmp/build
COPY docker/requirements/backend.txt requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    NIGHTSHIFT_REPO_PATH="/workspaces/nightshift"
WORKDIR /opt/nightshift
COPY --from=requirements /opt/venv /opt/venv
COPY . /opt/nightshift
COPY docker/backend-entrypoint.sh /usr/local/bin/nightshift-entrypoint
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git openssh-client bash tini curl python3-lgpio; \
    rm -rf /var/lib/apt/lists/*; \
    chmod +x /usr/local/bin/nightshift-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/nightshift-entrypoint"]
CMD ["python", "backend/server.py"]
