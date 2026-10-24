FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY prompts ./prompts
COPY mocks ./mocks
COPY scripts ./scripts
COPY samples ./samples

RUN useradd --create-home --uid 10001 agent
USER agent

EXPOSE 8000
# Default: API. docker-compose overrides the command for worker / mocks.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
