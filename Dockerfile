# The compile primitive, ghcr.io/uniconhq/primitive-compile. One container per
# compile step: it reads /work/inputs.json and the source under /work/in/, and
# writes /work/outputs.json and the binary under /work/out/.

# Pinned by digest so a release rebuilds from the same base. python:3.14-slim
# (Debian 13), pulled 2026-09-13, the same pin as the runner's images and the
# sandbox-run primitive, which runs what this image builds with the same Python
# and the same Java. Change it in both primitives together, from
# `docker image inspect python:3.14-slim --format '{{index .RepoDigests 0}}'`.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="primitive-compile" \
      org.opencontainers.image.description="The Unicon compile primitive: a folder of sources to one binary." \
      org.opencontainers.image.source="https://github.com/uniconhq/primitive-compile" \
      org.opencontainers.image.licenses="MIT"

# gcc and g++ link statically, so the binary runs in the sandbox-run image,
# which has no C library headers or compilers. The JDK's post-install scripts
# expect the man directory the slim image leaves out.
RUN mkdir -p /usr/share/man/man1 \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        gcc g++ libc6-dev openjdk-21-jdk-headless \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 src/compiler.py /usr/local/bin/compile

# The harness runs every step as a non-root user with a read-only root, and
# /work and /tmp as the only writable places. The program writes nowhere else.
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/compile"]
