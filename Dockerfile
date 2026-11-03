# Document Automation component - one image for cloud and on-prem.
FROM python:3.12-slim

# Option pack 1 system deps: Tesseract + English/Arabic language data (add more traineddata as needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Optional option-pack dependencies (build args keep the base image small)
ARG WITH_PADDLE=false
ARG WITH_AWS=false
ARG WITH_GOOGLE=false
RUN if [ "$WITH_PADDLE" = "true" ]; then pip install --no-cache-dir paddleocr paddlepaddle; fi && \
    if [ "$WITH_AWS" = "true" ]; then pip install --no-cache-dir boto3; fi && \
    if [ "$WITH_GOOGLE" = "true" ]; then pip install --no-cache-dir google-cloud-documentai; fi

COPY docauto ./docauto
COPY options ./options
COPY config ./config
ENV DOCAUTO_CONFIG=/app/config/document-service.yaml PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
EXPOSE 8085
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8085/health')"
CMD ["uvicorn", "docauto.api.app:app", "--host", "0.0.0.0", "--port", "8085"]
