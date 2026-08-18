FROM python:3.12-slim
RUN pip install --no-cache-dir fastmcp
WORKDIR /app
COPY server.py .
ENV CATALOG_DB=/data/mailroom.db \
    CATALOG_PORT=8766
EXPOSE 8766
CMD ["python", "server.py"]
