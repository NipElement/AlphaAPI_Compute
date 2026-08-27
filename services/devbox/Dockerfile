# arise/devbox — a tenant dev machine that a customer can actually SSH into.
#
# Until this image existed a "dev machine" was a sleep loop with a 20 KB log
# tail as its entire I/O surface. This is python:3.12-slim (the SAME digest as
# the catalog's python image) plus openssh-server, arranged so sshd runs as
# the unprivileged `dev` user (uid 65532 — the platform's tenant uid) on port
# 2222 under the restricted Pod Security Standard: no root, no capabilities,
# read-only rootfs. Writable paths are pod-provided emptyDirs: /home/dev,
# /keys, /tmp.
#
# Supply chain note: apt packages are pulled at BUILD time; the built image is
# then digest-pinned like every other artifact, and `make devbox-image` records
# the installed openssh-server version in the build log. Rebuilding is a
# deliberate act with a new digest, never an implicit drift.
FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends openssh-server openssh-client rsync; \
    dpkg -s openssh-server | grep '^Version' ; \
    rm -rf /var/lib/apt/lists/*; \
    # sshd insists the privilege-separation dir exists and is root-owned,
    # even when it will run unprivileged. Create it in the image (read-only
    # at runtime is fine — it only has to exist).
    mkdir -p /run/sshd && chmod 0755 /run/sshd; \
    # the tenant user, uid 65532 = what every tenant pod runs as
    groupadd -g 65532 dev && useradd -m -u 65532 -g 65532 -s /bin/bash dev; \
    # nothing in /etc/ssh may be writable by dev; keys live in /keys
    rm -f /etc/ssh/ssh_host_*_key*
COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/devbox-entrypoint
RUN chmod 0644 /etc/ssh/sshd_config && chmod 0755 /usr/local/bin/devbox-entrypoint
USER 65532:65532
WORKDIR /home/dev
EXPOSE 2222
ENTRYPOINT ["/usr/local/bin/devbox-entrypoint"]
