# 1. Traemos una computadora virtual que ya tiene Python instalado
FROM python:3.10-slim

# 2. Instalamos herramientas del sistema que NO son de Python
# Como usas pytesseract y pdf2image, necesitamos estos programas de Linux
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 3. Creamos una carpeta dentro de esa computadora virtual para tu proyecto
WORKDIR /app

# 4. Copiamos tu lista de librerías y las instalamos allá adentro
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copiamos el resto de tus archivos (tu main.py y otros) a la carpeta /app
COPY . .

# 6. Avisamos que el contenedor usará el puerto 8000
EXPOSE 8000

# 7. La instrucción final: "Cuando te enciendas, ejecuta este comando"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]