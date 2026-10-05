# Based on the official Docker CLI image (Alpine) so this container can
# drive the *host's* Docker daemon through the mounted socket
# (/var/run/docker.sock) to rebuild and restart the app service after a fix
# -- this already bundles the `docker compose` plugin.
FROM docker:27-cli

RUN apk add --no-cache python3 py3-pip nodejs npm git bash curl

# Headless coding agent CLI used to investigate and fix incidents.
# Override AGENT_CMD (see compose.observability.yaml) if you use a
# different agent/CLI.
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY requirements.txt .
RUN pip install --break-system-packages --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8001
CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
