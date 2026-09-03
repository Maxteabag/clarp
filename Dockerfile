# syntax=docker/dockerfile:1.7
FROM node:22.22.2-bookworm-slim AS node-runtime
ADD --checksum=sha256:5dbb86c71d07a1957f2e90734092dd6a58bdcd9ebc2d8d41ca1c6e6a21d364e1 \
    https://registry.npmjs.org/npm/-/npm-12.0.2.tgz /tmp/npm.tgz
RUN mkdir /tmp/npm-new \
    && tar -xzf /tmp/npm.tgz -C /tmp/npm-new \
    && rm -rf /usr/local/lib/node_modules/npm \
    && mv /tmp/npm-new/package /usr/local/lib/node_modules/npm \
    && rm -rf /tmp/npm.tgz /tmp/npm-new \
    && ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm --version

FROM node:22.22.2-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json vite.config.js ./
COPY web ./web
COPY static ./static
RUN npm ci --ignore-scripts --no-audit --no-fund && npm run build

FROM python:3.12-slim-bookworm AS runtime

# Bump these when a new model ships. A model rejects a CLI older than its own
# minimum ("does not support this model; version X or newer is required"), and
# a container user cannot fix that from inside: `stable` only publishes on a
# version tag, so whatever is pinned here is what they are stuck with until the
# next release. Updating means a new tag, a repull, and relaunching the running
# agents - a live agent keeps the CLI process it started with.
ARG CLAUDE_CODE_VERSION=2.1.259
ARG CODEX_VERSION=0.153.0
ARG CLARP_UPDATE_REMOTE=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLARP_DEPLOYMENT_MODE=container \
    CLARP_SHARE_DIR=/opt/clarp \
    CLARP_CODE_ROOT=/opt/clarp \
    PATH=/usr/local/bin:/usr/bin:/bin

COPY --from=node-runtime /usr/local/ /usr/local/

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg git gh iproute2 jq openssh-client sqlite3 tini \
    && npm install --global --allow-scripts=@anthropic-ai/claude-code \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
    && rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /opt/clarp
COPY requirements.txt requirements-docker.txt ./
RUN python -m pip install --no-cache-dir -r requirements-docker.txt \
    && python -m pip install --no-cache-dir uv==0.9.18

COPY server/server.py ./server.py
COPY server/lib ./lib
COPY --from=frontend /build/static ./static
COPY plugin ./plugin
COPY skills ./skills
COPY scripts ./scripts
COPY bin/clarp-admin.py ./bin/clarp-admin.py
COPY bin/clarp-tui.py ./bin/clarp-tui.py
COPY config.example.toml ./config.example.toml
COPY pyproject.toml ./pyproject.toml
COPY LICENSE.md COMMERCIAL_LICENSE.md ./
ARG CLARP_VERSION=dev
RUN printf '%s\n' "$CLARP_VERSION" > ./DEPLOYED_VERSION \
    && printf '%s\n' "$CLARP_UPDATE_REMOTE" > ./SOURCE_REMOTE
COPY docker/entrypoint.sh /usr/local/bin/clarp-entrypoint

ENV HOME=/home/clarp \
    CLARP_DATA_DIR=/data \
    CLARP_CONFIG_DIR=/data/clarp \
    CLAUDE_PWA_CONFIG=/data/clarp/config.toml \
    CLAUDE_PWA_DB=/data/clarp/state.sqlite \
    CLARP_INSTALL_STATE=/data/clarp/install.json \
    CLARP_CACHE_DIR=/tmp/clarp-cache \
    CLARP_CLAUDE_HOME=/data/claude \
    CLARP_CLAUDE_SKILLS=/data/claude/skills \
    CODEX_HOME=/data/codex \
    CLARP_CODEX_SKILLS=/data/codex/skills \
    CLARP_TRANSCRIPTION_MODELS=/data/models \
    CLARP_TRANSCRIPTION_REGISTRY=/data/models/transcription-models.json \
    CLARP_LOCAL_TTS_ROOT=/data/tts \
    HF_HOME=/data/models/huggingface \
    CLARP_MEDIA_DIR=/data/media \
    CLARP_UPLOADS_DIR=/data/uploads \
    CLARP_WORKSPACE_ROOT=/data/workspace \
    GH_CONFIG_DIR=/data/git/gh \
    CLAUDE_PWA_BIND=0.0.0.0 \
    CLARP_IMAGE_VERSION=${CLARP_VERSION}

RUN mkdir -p /data /tmp/clarp-cache /home/clarp \
    && chown -R 10001:10001 /data /tmp/clarp-cache /home/clarp \
    && chmod -R a+rX /opt/clarp \
    && chmod 755 /usr/local/bin/clarp-entrypoint /opt/clarp/bin/clarp-admin.py \
      /opt/clarp/bin/clarp-tui.py \
      /opt/clarp/scripts/agent_tasks.py \
      /opt/clarp/scripts/agent_artifacts.py \
      /opt/clarp/scripts/clarp-media-publish.py \
      /opt/clarp/scripts/agent_bg.py \
      /opt/clarp/scripts/github_workflow_artifact.py \
      /opt/clarp/skills/clarp-message-watch/scripts/watch_messages.py \
    && ln -s /opt/clarp/bin/clarp-admin.py /usr/local/bin/clarp-admin \
    && ln -s /opt/clarp/bin/clarp-tui.py /usr/local/bin/clarp-tui \
    && ln -s /opt/clarp/scripts/agent_tasks.py /usr/local/bin/clarp-agent-tasks \
    && ln -s /opt/clarp/scripts/agent_artifacts.py /usr/local/bin/clarp-agent-artifacts \
    && ln -s /opt/clarp/scripts/clarp-media-publish.py /usr/local/bin/clarp-media-publish \
    && ln -s /opt/clarp/scripts/agent_bg.py /usr/local/bin/clarp-agent-bg \
    && ln -s /opt/clarp/scripts/github_workflow_artifact.py /usr/local/bin/clarp-github-workflow-artifact \
    && ln -s /opt/clarp/skills/clarp-message-watch/scripts/watch_messages.py /usr/local/bin/clarp-message-watch

USER 10001:10001
VOLUME ["/data"]
EXPOSE 7682
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=4 \
  CMD python -c "import tomllib,urllib.request; c=tomllib.load(open('/data/clarp/config.toml','rb')); r=urllib.request.Request('http://127.0.0.1:7682/status',headers={'Authorization':'Bearer '+c.get('server',{}).get('auth_token','')}); urllib.request.urlopen(r,timeout=3)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/clarp-entrypoint"]
CMD ["python3", "/opt/clarp/server.py"]
