# Mock store API image (synthetic data). Build context: repo root.
FROM python:3.12.13-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi==0.162.7" "uvicorn==0.54.0"
COPY mock_store/ mock_store/
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
HEALTHCHECK --interval=3s --timeout=3s --retries=20 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "mock_store.app:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
