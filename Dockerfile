FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

# Create logs directory
RUN mkdir -p logs && chown appuser:appgroup logs

# Expose port
EXPOSE 8000

# Health check
# Targets /login rather than / : both are unauthenticated, but / issues a
# redirect, so a plain 200 keeps the check simple and unambiguous.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/login > /dev/null || exit 1

# Switch to non-root user
USER appuser

# Run the application
# --proxy-headers: honour X-Forwarded-* / CF-Connecting-IP from the reverse
#   proxy so the app sees the real client address. Without it, uvicorn exposes
#   only the proxy's IP and every visitor shares one rate-limit bucket.
# --forwarded-allow-ips="*" trusts whatever connects to this container. That is
#   safe ONLY if the origin is not directly reachable — firewall port 8000 to
#   your proxy (Cloudflare Tunnel, or Cloudflare's published IP ranges). If the
#   origin is exposed, an attacker can forge the header and evade rate limits.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
