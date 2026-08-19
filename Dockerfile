# syntax=docker/dockerfile:1
#
# One image, three roles.
#
# `agent`, `evals` and the CLI `client` are the same installed package invoked
# with three different commands. They share a dependency set, a settings
# object, a model layer and a database layer; the only thing that differs is
# the process entrypoint. Three near-identical Dockerfiles would be duplication
# wearing an architecture costume (BLUEPRINT §8), and it would also let the
# three drift apart — the eval harness must run against exactly the code the
# client talks to, or the eval stops being evidence.
#
# Two stages: the builder has uv and a compiler-free wheel install; the runtime
# has neither uv nor build tooling, just the virtualenv and the package.

# ---------------------------------------------------------------------------
# Stage 1 — build the virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# uv arrives as a static binary from its own image rather than via pip, so the
# runtime stage never inherits a Python-level installer.
COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Dependencies before source. They change on the order of once a month and the
# source changes every commit, so this layer survives almost every rebuild.
#
# `--locked` is the point of the lock file: it asserts uv.lock still matches
# pyproject.toml and fails the build if it does not, so the image can never be
# built against a resolution nobody reviewed.
#
# What actually keeps pytest and ruff out of the runtime image is that `dev` is
# an entry in [project.optional-dependencies] and `uv sync` installs no extras
# unless asked. `--no-dev` excludes PEP 735 dependency *groups*, of which this
# project has none, so it is belt to that braces: it costs nothing and it holds
# the line if `dev` ever moves into a group.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the package itself, built as a wheel rather than installed editable:
# the image should carry the code, not a path pointing at a build directory
# the runtime stage does not have.
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Non-root. The uid is fixed so a bind-mounted volume has predictable
# ownership across machines.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin trail

COPY --from=builder --chown=trail:trail /opt/venv /opt/venv

WORKDIR /app

# The approved collections script is baked in so the image runs standalone and
# so every image digest pins the exact protocol version it would speak. Compose
# also bind-mounts ./protocol read-only over this path, which lets a reviewer
# edit the regulated content and restart without a rebuild — the protocol is a
# git-versioned file, not a service (BLUEPRINT §8).
COPY --chown=trail:trail protocol/ /app/protocol/

USER trail

# Documentation, not a binding: one image serves the agent on 8000 and the
# evals harness on 8001 (INTERFACES §3, §4).
EXPOSE 8000 8001

# No entrypoint script. The role *is* the command, so compose selects it with
# `command:` and `docker compose run --rm client trail chat` works without a
# shim deciding what "client" means. This is the default for a bare
# `docker run`; every compose service overrides it explicitly.
CMD ["uvicorn", "trail.agent.app:app", "--host", "0.0.0.0", "--port", "8000"]
