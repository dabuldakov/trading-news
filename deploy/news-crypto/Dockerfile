FROM news-api:latest
WORKDIR /app
COPY src /app/src
COPY migrations /app/migrations
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "trading_news.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
