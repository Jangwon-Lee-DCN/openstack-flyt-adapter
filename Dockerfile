FROM python:3.12.11-slim@sha256:27f90d79cc85e9b7b2560063ef44fa0e9eaae7a7c3f5a9f74563065c5477cc24
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir '.[notifications]'
RUN install -d -o 65532 -g 65532 /var/lib/flyt-adapter
USER 65532:65532
ENTRYPOINT ["python", "-m", "flyt_adapter"]
CMD ["--config", "/etc/flyt-adapter/config.json"]
