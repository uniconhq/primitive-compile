# The compile primitive, ghcr.io/uniconhq/primitive-compile. One container per
# compile step: it reads /work/inputs.json and the source under /work/in/, and
# writes /work/outputs.json and the program under /work/out/.

# The image builds on the primitives' base, the runner's
# ghcr.io/uniconhq/primitive-base: the same python:3.14-slim, with the step
# wrapper as its entrypoint, which runs this image's command in a grading
# step and runs it in its own place anywhere else. BASE_IMAGE names the
# runner release's image; a build against a runner checkout passes the one
# built from it, as the tests and the shared CI do. exec and interact build
# on the same base, so they run what this image builds with its Python.
ARG BASE_IMAGE=ghcr.io/uniconhq/primitive-base:v0.10.0
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="primitive-compile" \
      org.opencontainers.image.description="The Unicon compile primitive: a folder of sources or a notebook to one program." \
      org.opencontainers.image.source="https://github.com/uniconhq/primitive-compile" \
      org.opencontainers.image.licenses="MIT"

# gcc and g++ link statically, so the program runs in the exec image,
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
CMD ["/usr/local/bin/compile"]
