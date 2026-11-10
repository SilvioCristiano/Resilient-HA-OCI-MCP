FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/mcp
COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd --uid 10001 --create-home mcp
COPY --chown=10001:10001 app /opt/mcp/app
USER 10001:10001

FROM runtime AS mcp
EXPOSE 8000
CMD ["python", "-m", "app.server"]

FROM runtime AS web
WORKDIR /opt/web
COPY --chown=10001:10001 web /opt/web
EXPOSE 8088
CMD ["python", "/opt/web/server.py"]
