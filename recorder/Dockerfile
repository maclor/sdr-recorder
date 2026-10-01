FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Official CPU-only DeepFilterNet3 binary.  It is a static musl build, so it
# also runs in this Debian-based image without pulling in Python/PyTorch.
ARG DEEPFILTER_VERSION=0.5.6
ARG DEEPFILTER_TARGET=x86_64-unknown-linux-musl
ARG DEEPFILTER_SHA256=70775e251eee44c0f2451a1e833326cf8bcbb304d3e7cd12851e6fce72ef7da
RUN curl --fail --location --retry 3 \
        -o /usr/local/bin/deep-filter \
        "https://github.com/Rikorose/DeepFilterNet/releases/download/v${DEEPFILTER_VERSION}/deep-filter-${DEEPFILTER_VERSION}-${DEEPFILTER_TARGET}" \
    && echo "${DEEPFILTER_SHA256}  /usr/local/bin/deep-filter" | sha256sum -c - \
    && chmod +x /usr/local/bin/deep-filter

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY recorder.py ./

CMD ["python", "-u", "recorder.py"]
