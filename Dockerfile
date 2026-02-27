FROM python:3.12-slim

WORKDIR /app

# System deps for pikepdf (qpdf) and PyMuPDF
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        qpdf \
        libmupdf-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY config.yaml .
COPY app/ app/

RUN mkdir -p uploads output templates

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
