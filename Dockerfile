FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/planbot.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY data ./data
# state lives in the /data volume so restarts keep plans; secrets come from the env file, never the image
VOLUME /data
CMD ["python", "bot.py"]
