FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data PORTAL_PORT=80 HTTPS_PORT=443 ADMIN_PORT=8081

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# lego: ACME client for the built-in Let's Encrypt support (checksum-pinned)
ARG TARGETARCH
COPY docker/install_lego.py /tmp/install_lego.py
RUN python /tmp/install_lego.py /usr/local/bin && rm /tmp/install_lego.py && lego --version

COPY app ./app

VOLUME /data
EXPOSE 80 443 8081
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ['PORTAL_PORT'])"
CMD ["python", "-m", "app.main"]
