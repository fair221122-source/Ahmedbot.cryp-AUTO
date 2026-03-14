FROM python:3.10

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080

# استبدل السطر الأخير بهذا
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8080"]
