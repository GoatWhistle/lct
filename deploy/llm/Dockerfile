# llama-server с весами Qwen3 и Vikhr — один образ на оба сервиса LLM.
#
# GGML_BACKEND_DL + GGML_CPU_ALL_VARIANTS: CPU-бэкенд собран в нескольких
# вариантах (от SSE4.2 до AVX512/AMX), нужный выбирается при запуске. Поэтому
# образ не падает SIGILL на CPU без AVX (стенд на QEMU) и не теряет скорость
# на AVX2/AVX512. Веса приходят именованным контекстом models=backend/models
# (docker-bake.hcl, docker-compose.yml), внутри сборки ничего не скачивается.
FROM ubuntu:24.04 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Тот же билд, что командный бинарник models/bin/llama-b10934
ARG LLAMA_TAG=b10934
RUN git clone --depth 1 --branch ${LLAMA_TAG} https://github.com/ggml-org/llama.cpp /src
RUN cmake -S /src -B /build \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=ON \
      -DGGML_NATIVE=OFF \
      -DGGML_BACKEND_DL=ON \
      -DGGML_CPU_ALL_VARIANTS=ON \
      -DLLAMA_CURL=OFF \
      -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
    && cmake --build /build --target llama-server -j "$(nproc)" \
    && mkdir -p /out \
    && cp /build/bin/llama-server /out/ \
    && find /build -name '*.so*' -exec cp -P {} /out/ \;

FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 curl ca-certificates && rm -rf /var/lib/apt/lists/*
# llama-server ищет libggml-cpu-*.so рядом с собой — всё в одном каталоге.
COPY --from=build /out/ /opt/llama/
ENV LD_LIBRARY_PATH=/opt/llama
COPY --from=models qwen3-1.7b/Qwen3-1.7B-Q8_0.gguf /models/qwen3-1.7b/
COPY --from=models vikhr-1b/Vikhr-Llama-3.2-1B-Q4_K_M.gguf /models/vikhr-1b/
COPY --from=licenses . /licenses
ENTRYPOINT ["/opt/llama/llama-server"]
