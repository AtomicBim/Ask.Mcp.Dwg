FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .
EXPOSE 8000
CMD ["python", "src/main.py"]
