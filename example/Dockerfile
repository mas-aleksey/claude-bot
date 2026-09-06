# A project sandbox: the base image plus whatever this project needs.
# A layer on top of claude-bot:latest, so shared dependencies are not duplicated.
#
# The base image has to exist before this builds:
#   docker compose --profile build build    # from the claude-bot repository
#
# Python, uv, psql and npm are NOT installed here — they are already in the base
# (python:3.14-slim + uv + postgresql-client + nodejs 22 with npm). A second python
# alongside the system one is only worth it when the project pins a different version.
FROM claude-bot:latest

# Put the project's own tooling here. A worked example — the GitLab CLI, whose artifact
# name contains the version, so a permalink will not do: ask the releases API for the tag
# first, then fetch the file. No pinned version: whatever is current at build time.
# The archive has no top-level directory (bin/glab sits at the root), so --strip-components
# is wrong here — it would cut off bin/ itself and drop the binary at /usr/local/glab.
#
# RUN V=$(curl -fsSL "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases?per_page=1" \
#           | sed -n 's/.*"tag_name":"v\([^"]*\)".*/\1/p') \
#  && test -n "$V" \
#  && curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${V}/downloads/glab_${V}_linux_amd64.tar.gz" \
#       | tar -xz -C /usr/local bin/glab \
#  && glab --version
