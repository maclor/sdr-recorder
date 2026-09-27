FROM python:3.11-slim-bookworm AS builder

# whisper.cpp is pinned so that builds are reproducible.
ARG WHISPER_VERSION=v1.9.4

# GGML_NATIVE=OFF compiles for baseline x86-64 instead of the build machine.
# ggml passes -march=native by default, which makes the binary depend on
# whatever CPU the GitHub Actions runner happened to be. The runner and the
# server have different CPUs, so swapping runners silently changes the image,
# and instructions outside AVX2 can raise SIGILL on the J1800.
# Set this to ON for ARM.
ARG GGML_NATIVE=OFF

RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${WHISPER_VERSION}" \
        https://github.com/ggml-org/whisper.cpp.git /whisper.cpp \
    && cmake -S /whisper.cpp -B /whisper.cpp/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_NATIVE="${GGML_NATIVE}" \
        -DGGML_BACKEND_DL=OFF \
        -DGGML_CCACHE=OFF \
        -DWHISPER_BUILD_IS_DEV=OFF \
    && cmake --build /whisper.cpp/build -j"$(nproc)" --target whisper-cli \
    && (command -v strip >/dev/null && strip /whisper.cpp/build/bin/whisper-cli || true)


FROM python:3.11-slim-bookworm

ARG WHISPER_VERSION=v1.9.4
ARG MODEL_FILE=ggml-base-q5_1.bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=builder /whisper.cpp/build/bin/lib*.so* /usr/local/lib/
RUN ldconfig

RUN mkdir -p /models \
    && curl -L --fail --retry 3 -o "/models/${MODEL_FILE}" \
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${MODEL_FILE}" \
    && printf 'whisper.cpp %s\nmodel %s\n' "${WHISPER_VERSION}" "${MODEL_FILE}" \
        > /models/BUILD-INFO

WORKDIR /app

COPY transcriber.py ./

CMD ["python", "-u", "transcriber.py"]
