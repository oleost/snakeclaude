FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py snake.html ./

ENV SNAKE_PORT=8934
ENV SNAKE_WS_PORT=8935
ENV SNAKE_DATA_FILE=/data/highscores.json

EXPOSE 8934
EXPOSE 8935
VOLUME ["/data"]

CMD ["python", "server.py"]
