# syntax=docker/dockerfile:1.7
# nginx image: TLS termination, security headers, WebSocket proxying, and the
# built terminal served at /app. Build context: repository root.

FROM node:24-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM node:24-alpine AS csp
WORKDIR /csp
COPY deploy/nginx/csp-hashes.mjs ./
COPY --from=frontend /build/out ./out
RUN node csp-hashes.mjs csp.conf $(find out -name '*.html')

FROM nginx:1.31-alpine
# OS security fixes released after the base image was built
RUN apk upgrade --no-cache && rm /etc/nginx/conf.d/default.conf
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY deploy/nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY deploy/nginx/proxy-headers.conf /etc/nginx/snippets/proxy-headers.conf
COPY --from=csp /csp/csp.conf /etc/nginx/snippets/csp.conf
COPY --from=frontend /build/out /usr/share/nginx/app
RUN mkdir -p /var/www/certbot
EXPOSE 80 443
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -q --spider http://127.0.0.1/nginx-health || exit 1
