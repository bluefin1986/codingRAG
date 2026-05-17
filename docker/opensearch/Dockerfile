# OpenSearch image for codingRAG keyword search.
# Keep this separate from the codingRAG app Dockerfile.

FROM opensearchproject/opensearch:2.19.1

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

# Install Chinese analyzer plugin for OpenSearch BM25 retrieval.
# Plugin version must match the OpenSearch base image version.
RUN /usr/share/opensearch/bin/opensearch-plugin install --batch \
    https://release.infinilabs.com/analysis-ik/stable/opensearch-analysis-ik-2.19.1.zip
