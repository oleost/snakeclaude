FROM python:3.12-slim

WORKDIR /app
COPY server.py snake.html ./

ENV SNAKE_PORT=8934
ENV SNAKE_DATA_FILE=/data/highscores.json

EXPOSE 8934
VOLUME ["/data"]

CMD ["python", "server.py"]
