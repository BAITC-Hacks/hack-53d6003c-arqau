FROM node:22-alpine AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CAREER_QUEST_DATA_DIR=/app/dataset

WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend/ ./backend/
COPY dataset/ ./dataset/
COPY --from=frontend-build /build/frontend/dist ./frontend/dist/
RUN python -m pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
