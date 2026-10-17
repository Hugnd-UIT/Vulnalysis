FROM kalilinux/kali-rolling

LABEL maintainer="Vulnalysis"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends -o Acquire::Retries=3 \
    ca-certificates \
    curl \
    wget \
    openvpn \
    git \
    unzip \
    tar \
    file \
    jq \
    build-essential \
    clang \
    cmake \
    ninja-build \
    meson \
    pkg-config \
    zlib1g-dev \
    cppcheck \
    gdb \
    strace \
    valgrind \
    binutils \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    python3-requests \
    sudo \
    && rm -rf /var/lib/apt/lists/*

RUN rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED

RUN mkdir -p /data && ln -s /data /workspace
WORKDIR /data

CMD ["/bin/bash"]