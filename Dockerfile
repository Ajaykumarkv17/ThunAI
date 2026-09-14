# ThunAI AgentCore Runtime container (arm64).
#
# AgentCore Runtime requires an arm64/linux image exposing the BedrockAgentCore
# app's /invocations (POST) and /ping (GET) endpoints. The app is served by
# `surface.entrypoint:app.run()` (see surface/entrypoint.py __main__).
#
# Built in the cloud via AWS CodeBuild's arm64 fleet (no local Docker needed).
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime dependencies first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the Python packages the runtime imports at execution time.
COPY agents/ ./agents/
COPY harness/ ./harness/
COPY integrations/ ./integrations/
COPY memory/ ./memory/
COPY policy/ ./policy/
COPY schemas/ ./schemas/
COPY surface/ ./surface/
COPY tools/ ./tools/
COPY triggers/ ./triggers/
COPY seed/ ./seed/

# AgentCore Runtime contract: the SDK serves on port 8080 by default.
EXPOSE 8080

CMD ["python", "-m", "surface.entrypoint"]
