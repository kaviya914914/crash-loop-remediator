FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir kopf kubernetes

COPY operator.py .

CMD ["kopf", "run", "operator.py", "--standalone", "--namespace=default"]