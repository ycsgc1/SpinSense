FROM python:3.11-slim

WORKDIR /app

# Both processes run from their own directory — the engine as a script from
# core/, the backend as a uvicorn app from gui/ — but share the `spinsense`
# package at the repository root, so the root must be importable from both.
ENV PYTHONPATH=/app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    portaudio19-dev \
    alsa-utils \
    libsndfile1 \
    gcc \
    ffmpeg \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements from the main folder
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy everything from the main project folder
COPY . .

# Make sure entrypoint is executable
RUN chmod +x ./docker/entrypoint.sh

EXPOSE 3313
ENTRYPOINT ["./docker/entrypoint.sh"]