FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY apps/backend/ /app/
RUN pip install .

EXPOSE 8000

CMD ["uvicorn", "devsupport_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
