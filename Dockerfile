# One image, reused by every service (web/api-ingest/ingest/backup/setup) via
# a different `command:` in docker-compose.yml. Environment comes from
# docker-compose's own `env_file: .env` on each service -- this image has no
# external secrets-manager dependency, unlike the native systemd reference
# deployment described in the README.
FROM python:3.12-slim

RUN groupadd --system omnimeter && useradd --system --gid omnimeter --no-create-home omnimeter

WORKDIR /opt/omnimeter

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY scripts/ scripts/
COPY templates/ templates/
COPY static/ static/
COPY wsgi.py devices.json.example ./

# /opt/omnimeter/data (DB + CSV dropzone) and any backup destination are bind
# mounts from docker-compose.yml, not baked into the image -- only their
# parent needs to exist and be writable by the container user up front.
RUN mkdir -p data && chown -R omnimeter:omnimeter /opt/omnimeter

USER omnimeter

# --no-control-socket: gunicorn's runtime control-socket feature defaults to
# $HOME/.gunicorn/, but the omnimeter user above has no home directory --
# every fresh `docker compose up -d` logged a startup "Permission denied"
# error otherwise. Nothing here uses gunicornc, so disable it outright.
CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:8000", "--no-control-socket", "wsgi:app"]
