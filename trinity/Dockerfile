FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY trinity/requirements.txt /app/trinity/requirements.txt
RUN pip install --no-cache-dir -r /app/trinity/requirements.txt
COPY trinity /app/trinity
RUN useradd -m trader && mkdir -p /app/trinity/data /app/trinity/logs && chown -R trader /app/trinity/data /app/trinity/logs
USER trader
# Paper mode only. Live trading needs an interactive console (scripts/run_live.py), never a container autostart.
CMD ["python", "trinity/scripts/run_paper.py"]
