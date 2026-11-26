FROM ghcr.io/linuxserver/orcaslicer:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip xvfb openscad \
    && pip3 install --no-cache-dir --break-system-packages fastapi uvicorn python-multipart requests \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY locales ./locales
COPY app.py /app/app.py
COPY extract_profiles.py /app/extract_profiles.py

ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
