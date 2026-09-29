FROM ghcr.io/astral-sh/uv:0.12.20 AS uv
FROM docker:cli AS docker
FROM postgres:17-trixie

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker

ENV UV_CACHE_DIR=/evidence/uv-cache \
    UV_PYTHON_INSTALL_DIR=/evidence/python \
    UV_PROJECT_ENVIRONMENT=/evidence/venv

WORKDIR /repo
ENTRYPOINT ["uv", "run", "--locked", "--python", "3.10", "dbreduce"]
CMD ["--help"]
