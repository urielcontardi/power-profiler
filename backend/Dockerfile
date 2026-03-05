FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libusb-1.0-0-dev \
    libudev-dev \
    pkg-config \
    tzdata \
    usbutils \
    udev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ /app/scripts/

RUN mkdir -p /app/logs && chmod +x /app/scripts/diagnose.sh

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
