FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN pip install --no-cache-dir --no-deps . && useradd --create-home --uid 10001 aegis
USER aegis
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1
CMD ["aegisops", "api", "--host", "0.0.0.0"]

