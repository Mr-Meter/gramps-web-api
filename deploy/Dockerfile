# Gramps Web image for the "Tree" fork (branch administrators + succession).
#
# Why a Dockerfile of our own: both the backend (gramps-web-api) and the
# frontend (gramps-web) are forks here, so the public ghcr.io/gramps-project/grampsweb
# images will not do: they have neither the new API nor the new screens. The
# frontend is built from source (Node 22, as in the upstream CI) and the final
# stage repeats the upstream production Dockerfile (gramps-web-api/Dockerfile),
# putting the built dist/ into /app/static instead of the empty index.html stub.
#
# The build context is the PARENT directory where both repositories sit side by side:
#   Tree/
#   ├── gramps-web/        frontend
#   └── gramps-web-api/    backend (this file is gramps-web-api/deploy/Dockerfile)
#
# Usually docker-compose.yml in this directory runs the build. By hand, from
# the Tree/ directory:
#   docker build -f gramps-web-api/deploy/Dockerfile -t tree-grampsweb:local .
# Context exclusions live in Dockerfile.dockerignore next to this file (read by
# BuildKit only, i.e. Docker >= 23 / docker compose v2).

ARG FRONTEND_DIR=gramps-web
ARG BACKEND_DIR=gramps-web-api

# ---------------------------------------------------------------------------
# Stage 1: frontend -> /build/dist
# ---------------------------------------------------------------------------
FROM node:22-alpine AS frontend
ARG FRONTEND_DIR
WORKDIR /build
# husky 4 tries to install git hooks on npm install, but .git is not in the context
ENV HUSKY_SKIP_INSTALL=1
COPY ${FRONTEND_DIR}/package.json ${FRONTEND_DIR}/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ${FRONTEND_DIR}/ ./
# API_URL unset => the frontend talks to the API of the same origin (AGENTS.md, "Build")
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: backend + static files (repeats the upstream gramps-web-api/Dockerfile)
# ---------------------------------------------------------------------------
FROM ghcr.io/gramps-project/gramps-web-base:latest
ARG BACKEND_DIR

ENV GRAMPS_API_CONFIG=/app/config/config.cfg

# create directories
RUN mkdir /app/src &&  mkdir /app/config && touch /app/config/config.cfg
RUN mkdir /app/static
RUN mkdir /app/db && mkdir /app/media && mkdir /app/indexdir && mkdir /app/users
RUN mkdir /app/thumbnail_cache
RUN mkdir /app/cache && mkdir /app/cache/reports && mkdir /app/cache/export \
    && mkdir /app/cache/request_cache && mkdir /app/cache/persistent_cache
RUN mkdir /app/tmp && mkdir /app/persist
# set config options
ENV GRAMPSWEB_USER_DB_URI=sqlite:////app/users/users.sqlite
ENV GRAMPSWEB_MEDIA_BASE_DIR=/app/media
ENV GRAMPSWEB_SEARCH_INDEX_DB_URI=sqlite:////app/indexdir/search_index.db
ENV GRAMPSWEB_STATIC_PATH=/app/static
ENV GRAMPSWEB_THUMBNAIL_CACHE_CONFIG__CACHE_DIR=/app/thumbnail_cache
ENV GRAMPSWEB_REQUEST_CACHE_CONFIG__CACHE_DIR=/app/cache/request_cache
ENV GRAMPSWEB_PERSISTENT_CACHE_CONFIG__CACHE_DIR=/app/cache/persistent_cache
ENV GRAMPSWEB_REPORT_DIR=/app/cache/reports
ENV GRAMPSWEB_EXPORT_DIR=/app/cache/export
ENV GRAMPSHOME=/root
ENV GRAMPS_DATABASE_PATH=/root/.gramps/grampsdb

# Model is baked into the base image - disable HF Hub network calls at runtime
ENV HF_HUB_OFFLINE=1

# copy package source and install
COPY ${BACKEND_DIR}/ /app/src
RUN pip install --no-cache-dir /app/src[ai,sentry]

# download and cache YFull tree for yclade
RUN python3 -c "import yclade; yclade.tree.download_yfull_tree()"

# the built frontend instead of upstream's empty /app/static/index.html
COPY --from=frontend /build/dist /app/static

EXPOSE 5000

COPY ${BACKEND_DIR}/docker-entrypoint.sh /
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD gunicorn -w ${GUNICORN_NUM_WORKERS:-8} -b 0.0.0.0:5000 gramps_webapi.wsgi:app --timeout ${GUNICORN_TIMEOUT:-120} --limit-request-line 8190
