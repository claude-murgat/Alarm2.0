FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN mkdir -p /app/data

# INV-164 : sha du commit et numero de release (fichier VERSION), exposes par /health
# (vides -> "dev"). Apres le COPY : le cache pip reste valable.
ARG GIT_SHA=
ENV GIT_SHA=$GIT_SHA
ARG APP_VERSION=
ENV APP_VERSION=$APP_VERSION

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
