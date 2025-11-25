FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Installa Node.js per buildare l'app React
RUN apt-get update && apt-get install -y \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Copia tutto il progetto
COPY . .

# Builda l'app React PRIMA di installare dipendenze Python
WORKDIR /app/weaviate-image-app
RUN npm install && npm run build

# Torna alla root e installa dipendenze Python
WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 10000
# Usa uvicorn direttamente per avviare l'app
# Render imposta PORT automaticamente, quindi usiamo uno script shell per leggerla
CMD sh -c "uvicorn serve:app --host 0.0.0.0 --port ${PORT:-10000}"
