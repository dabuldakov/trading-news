FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# IPv4-first: у контейнера нет IPv6-маршрута, а t.me отдаёт AAAA.
RUN echo "precedence ::ffff:0:0/96 100" > /etc/gai.conf

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY migrations ./migrations

EXPOSE 8000
CMD ["uvicorn", "trading_news.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
