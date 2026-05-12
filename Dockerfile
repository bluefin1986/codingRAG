FROM python:3.12-slim
WORKDIR /app

# System deps for building native extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV CODING_RAG_PRELOAD_DOMAINS=""
ENV CODING_RAG_QDRANT_HOST="qdrant"
ENV CODING_RAG_QDRANT_PORT="6333"

EXPOSE 8060

CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8060"]
