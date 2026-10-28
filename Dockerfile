FROM python:3.12-slim
WORKDIR /app
ARG CORE_REQUIREMENT
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY book_media_platform ./book_media_platform
RUN test -n "$CORE_REQUIREMENT" && pip install --no-cache-dir "$CORE_REQUIREMENT" && pip install --no-cache-dir .
RUN useradd --uid 10001 --create-home service
USER 10001
EXPOSE 8080
CMD ["python", "-m", "platform_media", "serve"]
