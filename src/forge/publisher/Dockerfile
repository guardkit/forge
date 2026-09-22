# ---------------------------------------------------------------------------
# The publisher's own image. NOT BUILT by the stage that wrote it, and not
# started by it: it is here so that the thing the design calls "its own
# process in its own container" has one, and so that the rollout has
# something to read rather than something to invent.
#
# WHY IT IS ITS OWN IMAGE AND NOT A FLAG ON THE COORDINATOR'S. The publisher
# holds the one credential that can write to a project's remote. Builds are
# launched with settings, and four of this estate's projects are built inside
# the coordinator today, so a credential in the coordinator's process is a
# credential in their builds. The wall is the process boundary, and an image
# of its own is what makes that boundary real.
#
# WHAT THIS IMAGE HAS, and nothing else: an interpreter, git, and Forge's own
# code. It has no docker socket, no sandbox, no project checkout, no build
# tool and no deploy step. It cannot build anything and it cannot run a
# project's own program.
#
# WHAT IT IS GIVEN AT RUN TIME:
#   * its settings file, read-only;
#   * the one file its credential is in, read-only, owned by its own user and
#     readable by nobody else — this is the file the design says must not be
#     readable from the coordinator's, the runner's or any sandbox's settings;
#   * the ledger, READ-ONLY;
#   * its own folder to keep its copies in.
#
# It listens on loopback. Nothing outside the host reaches it, and no sandbox
# does either: that is one of the five conditions the activation check
# refuses without (forge/src/forge/pipeline/publication_activation.py).
# ---------------------------------------------------------------------------

ARG PYTHON_BASE_DIGEST=sha256:2e256d0381371566ed96980584957ed31297f437569b79b0e5f7e17f2720e53a
FROM python:3.14-slim-bookworm@sha256:2e256d0381371566ed96980584957ed31297f437569b79b0e5f7e17f2720e53a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# git is the whole of the publisher's toolchain. Nothing else is added,
# because nothing else is used.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Its own user. The credential file is mounted owned by this user and
# readable by nobody else.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin publisher

WORKDIR /opt/forge
COPY --chown=publisher:publisher src/ /opt/forge/src/
ENV PYTHONPATH=/opt/forge/src

USER publisher

# Where it keeps its own bare copy of each project's commits, and the small
# program git asks for the credential through. Nothing else writes here.
ENV FORGE_PUBLISHER_STATE=/home/publisher/state
RUN mkdir -p /home/publisher/state

# Loopback only. The port is whatever the settings name.
EXPOSE 8711

ENTRYPOINT ["python", "-m", "forge.publisher"]
CMD ["--settings", "/etc/forge-publisher/settings.json"]
