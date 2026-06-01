FROM python:3.12-slim

WORKDIR /app

# Pre-create runtime directories so volume mounts inherit correct ownership
RUN mkdir -p /app/uploads /app/logs

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
