# Build Magic and Netgen from pinned commits.

ARG DEBIAN_DIGEST=debian@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241
ARG MAGIC_SHA=4432d7ec00ca744f5c9c3312662dece73ffbe83a
ARG NETGEN_SHA=bb8a6108b93b2d05538a976876f014f6c3a0269c

FROM ${DEBIAN_DIGEST} AS builder
ARG MAGIC_SHA
ARG NETGEN_SHA
ENV DEBIAN_FRONTEND=noninteractive

RUN printf 'Acquire::Retries "8";\nAcquire::http::Timeout "60";\n' \
      > /etc/apt/apt.conf.d/99-retries \
 && apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates git m4 \
      tcl-dev tk-dev libx11-dev libcairo2-dev libncurses-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Shallow clone at the exact SHA, and check that HEAD is that SHA before
# building. Magic and Netgen are run as subprocesses, never linked.
RUN mkdir -p netgen && cd netgen && git init -q \
 && git remote add origin https://github.com/RTimothyEdwards/netgen.git \
 && git fetch -q --depth 1 --no-tags origin "${NETGEN_SHA}" \
 && git checkout -q --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${NETGEN_SHA}" \
 && ./configure --prefix=/opt/sky130-verify/netgen > /build/netgen_configure.log 2>&1 \
 && make -j"$(nproc)" > /build/netgen_make.log 2>&1 \
 && make install >> /build/netgen_make.log 2>&1 \
 && test -x /opt/sky130-verify/netgen/bin/netgen

RUN mkdir -p magic && cd magic && git init -q \
 && git remote add origin https://github.com/RTimothyEdwards/magic.git \
 && git fetch -q --depth 1 --no-tags origin "${MAGIC_SHA}" \
 && git checkout -q --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${MAGIC_SHA}" \
 && ./configure --prefix=/opt/sky130-verify/magic > /build/magic_configure.log 2>&1 \
 && make -j"$(nproc)" > /build/magic_make.log 2>&1 \
 && make install >> /build/magic_make.log 2>&1 \
 && test -x /opt/sky130-verify/magic/bin/magic

FROM ${DEBIAN_DIGEST} AS runtime
ARG MAGIC_SHA
ARG NETGEN_SHA
ENV DEBIAN_FRONTEND=noninteractive

# Runtime dependencies:
# - git: sky130-verify reads the source repository and commit with git; without
#   it, provenance detection fails silently and --out resolves to the wrong base.
# - libtcl8.6, libx11-6, libcairo2: Magic loads tclmagic.so through Tcl `load`,
#   which needs libX11 and Cairo even in -dnull mode (not visible to `ldd` on
#   the main executable).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libtcl8.6 libx11-6 libcairo2 python3 python3-pip ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/sky130-verify/magic /opt/sky130-verify/magic
COPY --from=builder /opt/sky130-verify/netgen /opt/sky130-verify/netgen
ENV PATH="/opt/sky130-verify/magic/bin:/opt/sky130-verify/netgen/bin:${PATH}"

# Copy only the installable package and its metadata.
COPY pyproject.toml LICENSE README.md /opt/sky130-verify/src/
COPY sky130_verify /opt/sky130-verify/src/sky130_verify
RUN pip install --break-system-packages --no-cache-dir /opt/sky130-verify/src \
 && rm -rf /opt/sky130-verify/src

# Build provenance labels. `doctor` reads the installed versions at run time.
LABEL org.opencontainers.image.title="sky130-verify" \
      sky130-verify.magic.commit="${MAGIC_SHA}" \
      sky130-verify.netgen.commit="${NETGEN_SHA}"

# Non-root by default (uid/gid 1000): `check` needs no privilege, and files
# written to a mounted volume should not belong to root. Pass
# `--user $(id -u):$(id -g)` to `docker run` to use the host uid/gid.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin sky130-verify
USER sky130-verify
WORKDIR /home/sky130-verify

ENTRYPOINT ["sky130-verify"]
CMD ["--help"]
