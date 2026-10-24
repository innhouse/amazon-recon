# Hosted version of the web app (Cloud Run). The browser-based invoice fetch is
# not included here: run that on a PC with `python -m app.cli download-invoices`.
FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV RECON_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
