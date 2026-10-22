FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && groupadd --gid 10001 erp && useradd --uid 10001 --gid erp --no-create-home erp
COPY . .
RUN mkdir -p /app/data /app/staticfiles && chown -R erp:erp /app
USER erp
EXPOSE 8000
CMD ["sh", "scripts/entrypoint.sh"]
