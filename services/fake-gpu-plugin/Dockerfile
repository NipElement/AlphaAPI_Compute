# fake-gpu-plugin image — the only custom-built image in the lab.
#
# Supply-chain posture (plan §4.2):
#   - builder pinned by DIGEST (same golang:1.23-bookworm recorded as
#     GO_BUILDER_IMAGE in versions.env; resolved from registry-1.docker.io
#     2026-08-16);
#   - final stage is `scratch`: one static binary, no shell, no libc, nothing
#     to CVE-scan besides our own code and the two Go deps in go.mod;
#   - module list actually compiled is printed at build time (`go list -m all`)
#     so the build log kept under evidence/ records the full closure;
#   - the lab has no image registry, so the image reaches nodes via
#     `kind load docker-image` and the manifest uses imagePullPolicy: Never
#     with a content tag. Digest-in-manifest pinning applies on the dgx
#     overlay where a registry exists (and there this image is replaced by
#     NVIDIA's device plugin anyway — see overlays/lab/fake-gpu-plugin.yaml).
FROM golang@sha256:167053a2bb901972bf2c1611f8f52c44d5fe7e762e5cab213708d82c421614db AS build
WORKDIR /src
# go.sum is committed, so the build is reproducible: `go mod download` verifies
# every module against the recorded hashes (fails the build on mismatch) and
# `go build` re-verifies. NOT `go mod tidy`, which would re-resolve versions at
# build time and defeat the point of pinning.
COPY go.mod go.sum main.go ./
RUN go mod download && go list -m all && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/fake-gpu-plugin .

FROM scratch
COPY --from=build /out/fake-gpu-plugin /fake-gpu-plugin
USER 0
ENTRYPOINT ["/fake-gpu-plugin"]
