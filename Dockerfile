# Multi-arch image: linux/amd64, linux/arm64 (armv7 is not a release target).
# The builder stage is kept on purpose: uvicorn[standard] pulls httptools / uvloop / watchfiles and
# pydantic-core (all compiled). If a wheel is missing for an architecture, pip can fall back to a
# source build there; the final stage only receives the installed packages.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libyaml-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install the package itself (with its runtime dependencies, without dev extras)
COPY pyproject.toml README.md LICENSE THIRD-PARTY-NOTICES /src/
COPY sme_ocpp_sim /src/sme_ocpp_sim
COPY configs /src/configs
COPY scenarios /src/scenarios
RUN pip install --prefix=/install /src


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# libyaml for the PyYAML C extension when it was compiled in the builder stage;
# tzdata so `-e TZ=Asia/Tokyo` sets the clock used by the UI and charging schedules (default: UTC)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libyaml-0-2 tzdata \
    && rm -rf /var/lib/apt/lists/*

# Also installs the `sme-ocpp-sim` console script into /usr/local/bin
COPY --from=builder /install /usr/local

WORKDIR /app
COPY configs ./configs
COPY THIRD-PARTY-NOTICES LICENSE ./

# Run as an unprivileged user
RUN useradd --create-home --uid 10001 sim && chown -R sim /app
USER sim

# Web UI only. There is no protocol port: the simulated charge points are WebSocket *clients* that
# connect out to the CSMS. To reach a CSMS running on the Docker host, use
# ws://host.docker.internal:<port>/... as the CSMS URL (Docker Desktop provides that name; on Linux add
# `--add-host=host.docker.internal:host-gateway` to `docker run`). The bundled demo config points at
# ws://localhost:8180 (SteVe), which inside the container is the container itself: change the URL in the
# web UI, or mount your own config, e.g.
#   docker run -p 8082:8082 -v "$PWD/site.yaml:/app/configs/site.yaml" IMAGE \
#     sme-ocpp-sim run -c configs/site.yaml --host 0.0.0.0 --no-browser
#
# Exception: the built-in Mini CSMS (`mini_csms:` block, off by default) is a WebSocket *server*. To let
# stations outside the container connect to it, enable it on all interfaces and publish its port, e.g.
#   docker run -p 8082:8082 -p 9000:9000 IMAGE sme-ocpp-sim run -c configs/mini-csms-demo.yaml #     --host 0.0.0.0 --mini-csms --csms-host 0.0.0.0 --no-browser
EXPOSE 8082/tcp
EXPOSE 9000/tcp

CMD ["sme-ocpp-sim", "run", "-c", "configs/demo-site.yaml", "--host", "0.0.0.0", "--http-port", "8082", "--no-browser"]
