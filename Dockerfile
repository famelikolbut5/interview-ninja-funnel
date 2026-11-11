FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN python -c "import subprocess, tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; subprocess.run(['python', '-m', 'pip', 'install', '--no-cache-dir', '-c', 'requirements.lock', *deps], check=True)"
COPY launch_funnel ./launch_funnel
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/logs \
    && chown -R app:app /app/data /app/logs
COPY funnel.yaml ./
USER 10001:10001
ENTRYPOINT ["python", "-m"]
CMD ["launch_funnel.bot"]

FROM runtime AS test
USER root
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY tests ./tests
USER 10001:10001
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
