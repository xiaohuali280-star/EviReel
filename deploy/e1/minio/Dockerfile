# MinIO server for the E1 dev/CI stack.
# minio/minio and minio/mc are no longer published on Docker Hub (the repositories return 404), so the
# image is built from the conda-forge minio-server package - the same release the Windows native setup
# uses - pinned by version and sha256. curl is kept for the health check.
FROM debian:12.12-slim
ARG MINIO_PKG=minio-server-2025.10.15.17.29.55-hbcca054_0
ARG MINIO_SHA256=5be30a904d2f5d5defa3be69c9f8fa519fde68f907049e351028727c90a5068e
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip zstd; \
    cd /tmp; \
    curl -fsSL -o minio.conda "https://conda.anaconda.org/conda-forge/linux-64/${MINIO_PKG}.conda"; \
    echo "${MINIO_SHA256}  minio.conda" | sha256sum -c -; \
    unzip -q minio.conda "pkg-${MINIO_PKG}.tar.zst"; \
    mkdir pkg; tar --zstd -xf "pkg-${MINIO_PKG}.tar.zst" -C pkg; \
    install -m 0755 pkg/bin/minio /usr/local/bin/minio; \
    rm -rf /tmp/*; \
    apt-get purge -y unzip zstd; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*; \
    minio --version
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
