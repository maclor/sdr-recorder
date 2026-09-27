FROM python:3.11-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git /whisper.cpp \
    && cd /whisper.cpp \
    && cmake -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build -j1 --target whisper-cli


FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=builder /whisper.cpp/build/bin/lib*.so* /usr/local/lib/
RUN ldconfig

RUN mkdir -p /models \
    && curl -L --fail -o /models/ggml-base-q5_1.bin \
        https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base-q5_1.bin

WORKDIR /app

COPY transcriber.py .

CMD ["python", "-u", "transcriber.py"]