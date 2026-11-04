# claude-agent tools image — OpenCode and Pi on top of claude-agent:base.
# Built by start-agent.sh as claude-agent:latest, after claude-agent.Dockerfile
# has been built as claude-agent:base.
#
# Why two Dockerfiles (ADR-054): --refresh-tools must rerun only the npm
# install. With one Dockerfile that depended on docker's build cache keeping
# every layer above it, and on the host (legacy builder, no buildx) the cache
# did not hold: the second refresh rebuilt 12 of 13 layers. Building FROM the
# tagged base image needs no cache at all. The base is a local tag, so docker
# uses it without a pull. If :base is missing, docker tries to pull it from
# Docker Hub and fails; start-agent.sh builds it first to prevent that.
# Keep this file to the npm install: any layer added here reruns on every
# refresh. New layers belong in claude-agent.Dockerfile.

FROM claude-agent:base

# ── OpenCode + Pi CLIs ───────────────────────────────────────────────────────
# Both shipped as canonical npm packages: @opencode/cli (binary `opencode`) and
# @earendil-works/pi-coding-agent (binary `pi`, requires Node >=22.19.0 which
# setup_lts.x satisfies). Installed in one layer so npm bootstraps once.
# OpenCode 2.x is @opencode/cli; opencode-ai only carries the 1.x line, so
# opencode-ai@latest never reaches 2.x (ADR-056). @opencode/cli's postinstall
# swaps its placeholder bin for the platform binary, so it must be allowed:
# without it `opencode` only prints "postinstall script was not run".
# TOOLS_CACHE_BUST: `start-agent.sh --refresh-tools` passes a fresh value so
# this layer reruns (pulling the current @latest). A plain build leaves it at
# 0, so a cached npm layer, if the builder has one, is reused.
ARG TOOLS_CACHE_BUST=0
RUN echo "tools cache bust: ${TOOLS_CACHE_BUST}" \
 && npm install -g --allow-scripts=@opencode/cli,@google/genai,protobufjs @opencode/cli@latest @earendil-works/pi-coding-agent@latest

# WORKDIR /root and CMD come from the base: nothing here may add a layer
# besides the npm install (the legacy builder commits a WORKDIR step).
