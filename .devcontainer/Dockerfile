# Dev environment for CONTRIBUTORS working on the tool itself -- not intended
# for running the tool against a real manual archive (see CONTRIBUTING.md for
# why: --in-place writes back to the exact host path it read from, which needs
# the container's mount to line up with wherever your files actually live;
# for that, just run the script directly per the README).
FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm

# Ghostscript is apt-installable; the jbig2 CLI is not (Debian/Ubuntu only ship
# the libjbig2enc-dev/libjbig2enc0t64 *library*, confirmed against the actual
# package repos), so it's built from source here -- the same recipe validated
# in .github/workflows/ci.yml. tesseract-ocr is added too (unlike CI, which
# skips it: every test passes ocr=False, but a contributor running the real
# tool interactively will want working OCR).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ghostscript \
        tesseract-ocr \
        build-essential autoconf automake libtool libleptonica-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/agl/jbig2enc.git /tmp/jbig2enc \
    && cd /tmp/jbig2enc \
    && ./autogen.sh && ./configure && make -j"$(nproc)" && make install \
    && cd / && rm -rf /tmp/jbig2enc

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt pytest
