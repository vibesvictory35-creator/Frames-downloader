FROM python:3.11-slim

# curl+unzip for the Deno installer, ffmpeg for scene detection + frame extraction
RUN apt-get update && apt-get install -y curl unzip ffmpeg && rm -rf /var/lib/apt/lists/*

# Deno: JS runtime yt-dlp uses to run YouTube's challenge-solver scripts
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port $PORT"]
