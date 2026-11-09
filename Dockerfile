# Larkfield ticket desk — production image.
#
# The image is built from the local checkout, so data_room/ is present in the
# build context even though it is git-ignored.  prepare runs at build time and
# writes /app/prepared, which the service reads at start-up.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_ROOM_DIR=/app/data_room \
    PREPARED_DIR=/app/prepared \
    DATABASE_URL=sqlite:////data/runs.db \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY data_room ./data_room

RUN python -m src.prepare

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

CMD ["sh", "-c", "uvicorn src.app:create_app --factory --host 0.0.0.0 --port ${PORT}"]
