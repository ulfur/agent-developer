FROM alpine:3.20 AS assets
WORKDIR /tmp/frontend
COPY frontend/ ./

FROM nginx:1.27-alpine AS runtime
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=assets /tmp/frontend /usr/share/nginx/html
COPY docker/frontend-selfcheck.sh /usr/local/bin/frontend-selfcheck
RUN chmod +x /usr/local/bin/frontend-selfcheck
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
