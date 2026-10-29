FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
ENV MINTSCOPE_DB_PATH=/data/mintscope.db
EXPOSE 8000
# Secrets (OPENSEA_API_KEY etc.) come from the host's environment, never from the image.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
