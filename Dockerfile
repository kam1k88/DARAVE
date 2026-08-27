# Образ backend'а (server.py) — облачная часть DARAVE.
# companion (companion_main.py) сюда НЕ входит — он живёт на компьютере
# диджея рядом с Mixxx и в докере не запускается.
FROM python:3.12-slim

WORKDIR /app

# python-rtmidi backend'у не нужен (он только у companion) — ставим
# зависимости отдельным списком, без "лишнего" rtmidi/websockets-клиента,
# чтобы не тащить лишние системные библиотеки (librtmidi-dev и т.п.) в образ.
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY server.py session.py agent.py persistence.py mixplan.py ./
COPY static ./static

RUN mkdir -p /app/recordings
ENV DARAVE_DB_PATH=/app/data/darave.db
VOLUME ["/app/data", "/app/recordings"]

EXPOSE 8765
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
