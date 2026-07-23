# ── Stage 1: builder ──────────────────────────────────────────────────────────
# Install all dependencies into /root/.local so they can be copied cleanly
# to the runtime stage without pip cache, build tools, or intermediate layers.
FROM python:3.13-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
# Minimal image — only the app code and installed packages.
# Final image size: ~180MB (vs ~600MB with a single-stage full build).
FROM python:3.13-slim

LABEL org.opencontainers.image.title="URL Shortener API"
LABEL org.opencontainers.image.description="Distributed URL shortener — FastAPI service"

WORKDIR /app

# Copy installed packages from the builder stage (no pip cache included)
COPY --from=builder /root/.local /root/.local

# Copy application source (only what's needed at runtime)
COPY app/ ./app/
COPY dashboard/ ./dashboard/

# Ensure scripts installed by pip are on PATH
ENV PATH=/root/.local/bin:$PATH

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# --workers 4 matches the Phase 5 load-test fix:
# 4 independent processes → 4 GIL domains → 4 CPU cores utilised.
# Each worker gets its own SQLAlchemy pool (20 base + 40 overflow = 60 per worker).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
