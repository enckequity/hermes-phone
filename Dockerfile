# One container for Railway: Hermes gateway (Telegram) + hermes-phone plugin + media bridge.
# Base image internals (verified in hermes-agent source): s6-overlay is PID 1, HERMES_HOME=/opt/data
# (VOLUME), venv at /opt/hermes/.venv, runtime user "hermes" (uid 10000, dropped to by s6).
FROM nousresearch/hermes-agent:v0.22.7

USER root

# Plugin source, synced into $HERMES_HOME/plugins/hermes-phone on every boot (03-hermes-phone).
COPY plugin.yaml __init__.py pyproject.toml README.md /opt/hermes-phone/
COPY hermes_phone/ /opt/hermes-phone/hermes_phone/
COPY skills/ /opt/hermes-phone/skills/
COPY deploy/boot-config.py /opt/hermes-phone-boot/boot-config.py

# Plugin python deps into Hermes' venv (Hermes never auto-installs python_dependencies). uv comes
# from Hermes' pinned toolchain store (pm._uv, private API: the image bakes uv at build time). Constraints = what the venv already has, so Hermes' own pins
# can never move; a conflict fails the build here (and in CI) instead of at runtime.
RUN set -eu; \
    UV="$(cd /opt/hermes && python3 -c 'from pm._uv import _toolchain; print(_toolchain(realize=False)[0])')"; \
    PY=/opt/hermes/.venv/bin/python; \
    "$UV" pip freeze --python "$PY" > /tmp/constraints.txt; \
    "$PY" -c 'import tomllib; print("\n".join(tomllib.load(open("/opt/hermes-phone/pyproject.toml","rb"))["project"]["dependencies"]))' > /tmp/requirements.txt; \
    "$UV" pip install --no-cache --python "$PY" -r /tmp/requirements.txt -c /tmp/constraints.txt; \
    PYTHONPATH=/opt/hermes-phone "$PY" -c 'import twilio, phonenumbers, fastapi, uvicorn, websockets, httpx, tzdata, hermes_phone.bridge.app'; \
    rm -f /tmp/constraints.txt /tmp/requirements.txt

COPY --chmod=0755 deploy/03-hermes-phone /etc/cont-init.d/03-hermes-phone
COPY deploy/s6/hermes-phone-bridge/ /etc/s6-overlay/s6-rc.d/hermes-phone-bridge/
RUN chmod 0755 /etc/s6-overlay/s6-rc.d/hermes-phone-bridge/run \
    && touch /etc/s6-overlay/s6-rc.d/user/contents.d/hermes-phone-bridge

# Both processes use this one SQLite file (plugin and bridge share state only through it).
ENV HERMES_PHONE_QUIET_AGENT=1
ENV HERMES_PHONE_DB=/opt/data/plugin-data/hermes-phone/data.db

# Image idiom: CMD "gateway run" starts the s6-supervised gateway (auto-restart) and keeps the
# container alive; Telegram uses long polling, so no inbound port is needed for it.
CMD ["gateway", "run"]
