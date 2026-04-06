FROM ghcr.io/spiffe/spire-agent:1.14.1 AS spire
FROM python:3.12-slim

# Copy spire-agent binary so the workload can call the Workload API
COPY --from=spire /opt/spire/bin/spire-agent /opt/spire/bin/spire-agent

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

CMD ["python", "-u", "app.py"]
