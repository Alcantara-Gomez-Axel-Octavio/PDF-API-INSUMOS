# 1. Traemos una computadora virtual que ya tiene Python instalado
FROM python:3.10-slim

# 2. Instalamos herramientas del sistema
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 3. Carpeta de trabajo
WORKDIR /app

# 4. Instalación de librerías
# RECUERDA: Agrega 'gunicorn' a tu requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copiamos el código
COPY . .

# 6. Puerto
EXPOSE 8000

# 7.INSTRUCCIÓN DE ARRANQUE (GUNICORN + RECYCLING)
# -w 2: Usamos solo 2 trabajadores para no saturar los 1.7GB de RAM.
# -k uvicorn.workers.UvicornWorker: El motor para FastAPI.
# --max-requests 2: Reinicia el trabajador cada 2 PDFs procesados.
# --max-requests-jitter 2: Evita que todos los trabajadores reinicien al mismo tiempo.
# --timeout 120: Tiempo de espera para procesos pesados de OCR.
CMD ["gunicorn", "main:app", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "--max-requests", "2", "--max-requests-jitter", "2", "--bind", "0.0.0.0:8000", "--timeout", "120"]