FROM python:3.11-slim

# Install curl + unzip (needed for Deno installer) and ffmpeg (needed for yt-dlp merging formats)
RUN apt-get update && apt-get install -y curl unzip ffmpeg && rm -rf /var/lib/apt/lists/*

# Install Deno (JS runtime yt-dlp needs to solve YouTube's challenge)
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# adjust this to however you start your app
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port $PORT"]
