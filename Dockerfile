# Job Application Bot — one image, two runtime roles (endpoint + pipeline).
# See job_automation_architecture.md, Iteration 5 (Production Deployment).
#
# Build this ON THE TARGET (the Oracle VM or your local box) so pip resolves
# wheels for the host architecture.
#
# Arch note: requirements.txt pins `torch==2.12.0+cpu` (an x86_64 wheel). That
# builds on x86_64 hosts (incl. local WSL2 and the AMD micro shape). On an
# arm64 shape (Ampere A1) the `+cpu` wheel won't resolve — switch the torch
# line to plain `torch==2.12.0` (PyPI's aarch64 build is already CPU-only), or
# make it arch-aware with environment markers. See the architecture doc.
FROM python:3.11-slim

# LibreOffice headless is the one non-pip dependency (DOCX -> PDF, Layer 6).
# src/endpoint/pdf_convert.py resolves `soffice` via shutil.which() on PATH, so
# no code change is needed.
#
# Fonts: the resume template uses Arial. Arial is a Microsoft font not shipped
# with Linux, so without it LibreOffice substitutes Liberation Sans and the
# render differs subtly from the template as seen in Word. ttf-mscorefonts-
# installer pulls the real Arial/Times/etc. (free, but Microsoft-EULA — accepted
# non-interactively below). It lives in Debian "contrib", enabled here.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/^Components: .*/& contrib/' /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's/ main/ main contrib/g' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libreoffice-writer \
        fonts-dejavu \
        fonts-liberation; \
    echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
        | debconf-set-selections; \
    apt-get install -y --no-install-recommends ttf-mscorefonts-installer; \
    fc-cache -f; \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/cache/hf

WORKDIR /app

# Dependency layer first so code changes don't bust the pip cache. This single
# install also provisions the spaCy model wheel + CPU torch (both pinned in
# requirements.txt) — no separate download steps.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code. Per-operator secrets and profile files (.env,
# master_profile.*, google_service_account.json) are EXCLUDED via .dockerignore
# and bind-mounted at runtime by docker-compose — never baked into the image
# (rule #18: no credentials in the image).
COPY . .

EXPOSE 8000

# Default role: the always-on resume endpoint. The `pipeline` service in
# docker-compose overrides this command with `python -m src.main`.
CMD ["uvicorn", "src.endpoint.app:app", "--host", "0.0.0.0", "--port", "8000"]
