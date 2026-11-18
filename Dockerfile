FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data/sessions /app/uploads /app/static
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
