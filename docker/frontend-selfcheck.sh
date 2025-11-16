#!/usr/bin/env sh
set -e
if [ ! -f /usr/share/nginx/html/index.html ]; then
    echo "frontend assets missing" >&2
    exit 1
fi
echo "frontend assets ready"
