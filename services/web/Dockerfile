# arise/web — the built SPA (web/dist) as a content image.
#
# WHY THIS EXISTS: the lab overlay delivers the SPA by `docker cp web/dist`
# into the kind control-plane node and hostPath-mounts it into the gateway.
# That is a kind-ism — there is no `docker exec <node>` on real DGX hardware.
# On dgx, this image carries the dist and the gateway's initContainer copies it
# into an emptyDir the gateway serves from. Same gateway container, no hostPath,
# no docker cp — a registry pull instead.
#
# Base pinned by digest (busybox:1.37-musl, resolved from registry-1.docker.io).
# The image is just static files + one shell for the initContainer's `cp`;
# nothing here listens or runs as a long process.
#
# The gateway's initContainer runs non-root (uid 65532), so the staged files
# must be world-readable: chmod a+rX after COPY. dirs get +x for traversal,
# files get +r, nothing becomes writable by non-owners.
FROM busybox@sha256:fc6dddc4c44b1bfe37f41cae8e67d1693828e8f42a91862816d7953e2c9d3f23
COPY dist/ /web/
RUN chmod -R a+rX /web
# initContainer entrypoint: populate the shared volume, then exit.
# `cp -r` (not -a): the non-root initContainer cannot preserve root ownership,
# and does not need to — the gateway only reads these files.
CMD ["sh","-c","cp -r /web/. /srv/web/ && echo web-staged"]
