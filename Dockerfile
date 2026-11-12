FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
RUN useradd --create-home --uid 1000 bot && mkdir -p /data && chown bot:bot /data
USER bot

CMD ["python", "-u", "bot.py"]
