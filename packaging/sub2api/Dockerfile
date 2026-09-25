FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE UPSTREAM-LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir --disable-pip-version-check --timeout 120 --retries 5 . \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid bridge --no-create-home bridge
USER 10001:10001
EXPOSE 8000
CMD ["excel-sub2api", "serve", "--host", "0.0.0.0", "--port", "8000"]
