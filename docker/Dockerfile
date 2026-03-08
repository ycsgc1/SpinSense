FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    portaudio19-dev \
    alsa-utils \
    libsndfile1 \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements from the main folder
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy everything from the main project folder
COPY . .

# Make sure entrypoint is executable
RUN chmod +x ./docker/entrypoint.sh

ENTRYPOINT ["./docker/entrypoint.sh"]