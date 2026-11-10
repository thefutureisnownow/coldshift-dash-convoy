# Build from the repository's root, so the site goes in too:
#   docker build -f lan/Dockerfile -t coldshift-dash-convoy .
FROM python:3.12-slim
WORKDIR /app
COPY lan/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lan/convoy_server.py .
COPY site ./site
# convoys (token, name, password hash, the starter's key hash) are kept here across restarts
RUN mkdir /data && chown nobody /data
ENV PORT=8080 DATA_DIR=/data SITE_DIR=/app/site
VOLUME /data
EXPOSE 8080 47660/udp
USER nobody
CMD ["python", "convoy_server.py"]
