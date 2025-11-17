# syntax=docker/dockerfile:1.6

FROM python:3.11-slim AS requirements
ENV PYTHONUNBUFFERED=1
WORKDIR /tmp/build
COPY docker/requirements/backend.txt requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim AS runtime
ARG NIGHTSHIFT_CAPABILITIES="cloud"
ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    NIGHTSHIFT_REPO_PATH="/workspaces/nightshift" \
    NIGHTSHIFT_CAPABILITIES="${NIGHTSHIFT_CAPABILITIES}"
WORKDIR /opt/nightshift
COPY --from=requirements /opt/venv /opt/venv
COPY . /opt/nightshift
COPY docker/backend-entrypoint.sh /usr/local/bin/nightshift-entrypoint
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git openssh-client bash tini curl; \
    CAPS=",$NIGHTSHIFT_CAPABILITIES,"; \
    if printf '%s' "${CAPS}" | grep -Eq ",(gpio|eink|hardware),"; then \
        if apt-cache show python3-lgpio >/dev/null 2>&1; then \
            apt-get install -y --no-install-recommends python3-lgpio; \
        else \
            echo "Requested gpio capability but python3-lgpio is unavailable for this base image; skipping install." >&2; \
        fi; \
    fi; \
    rm -rf /var/lib/apt/lists/*; \
    chmod +x /usr/local/bin/nightshift-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/nightshift-entrypoint"]
CMD ["python", "backend/server.py"]
