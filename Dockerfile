FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MFP_DB=/data/mfp.db

WORKDIR /app
COPY pyproject.toml ./
COPY mfp_connector ./mfp_connector
RUN pip install . && mkdir -p /data

EXPOSE 8000
ENTRYPOINT ["python", "-m", "mfp_connector"]
CMD ["serve"]
