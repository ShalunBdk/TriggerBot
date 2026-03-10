FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trigger_bot.py .

VOLUME /app/data
ENV DB_PATH=/app/data/triggers.db

CMD ["python", "trigger_bot.py"]