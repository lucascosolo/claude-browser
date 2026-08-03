# Base is pinned by digest, not by tag: `trixie-slim` is a moving target and a
# proxy that every page the browser loads talks to is not something we want
# silently rebuilt on top of a different userland.
FROM debian@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd

# tinyproxy comes from Debian rather than from a third-party image because the
# HTTP parser here faces hostile input (every page the user visits is a client of
# it). A distro package gets security updates from a team we already trust for
# the rest of the userland; a random Docker Hub tinyproxy image gets them from
# whoever still has the account.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tinyproxy ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Not /etc/tinyproxy: that path is a tmpfs at runtime (see docker-compose.yml) so
# the rendered config never lands on disk, and a tmpfs mount hides whatever the
# image put underneath it.
COPY tinyproxy.conf /opt/cb-vpn/tinyproxy.conf.tmpl
COPY filter.deny    /opt/cb-vpn/filter.deny
COPY entrypoint.sh  /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

EXPOSE 8888

# The rendered config carries the proxy password, so it is written at runtime
# into the container's own filesystem rather than baked into an image layer that
# `docker history` would happily read back out.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
