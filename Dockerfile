FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dblab_agent ./dblab_agent
COPY web ./web

EXPOSE 8000

CMD ["uvicorn", "dblab_agent.server:app", "--host", "0.0.0.0", "--port", "8000"]
