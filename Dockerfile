FROM python:3.12-slim
WORKDIR /app

# Use a more stable Debian mirror and retry apt downloads.
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=5 \
    && apt-get install -y --no-install-recommends -o Acquire::Retries=5 \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
COPY . .

ENV CODING_RAG_PRELOAD_DOMAINS=""
ENV CODING_RAG_QDRANT_HOST="qdrant"
ENV CODING_RAG_QDRANT_PORT="6333"

EXPOSE 8060

CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8060"]
