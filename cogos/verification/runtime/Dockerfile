# The runtime the subject executes in. Deliberately minimal: an interpreter and a test runner, and
# nothing that would let a failure here be blamed on the image. Every version is pinned, so a
# receipt naming this image names one filesystem and rebuilding it is reproducible.
#
# Build with `cogos/verification/runtime/build.sh` (or `docker build -t cogos-verifier:1 .` here).
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

# Optional, and optional on purpose: in an environment whose egress goes through a TLS-inspecting
# proxy, pip needs that proxy's CA to verify PyPI. The glob makes the file optional so the build
# works unchanged on a normal network, and `build.sh` supplies it when one is configured. It is a
# public certificate rather than a credential, it is removed in the same layer, and the runtime
# container has no network at all. TLS verification is never disabled — a build that trusts
# anything is a supply chain that trusts anything.
COPY proxy-ca.crt* /tmp/

# pytest is here because the acceptance contract runs the deliverable's own suite inside the
# boundary. It is pinned: an unpinned runner would make "the suite passed" a statement about
# whichever version happened to resolve that day.
#
# No `|| true` at the end of this. A trailing `|| true` made a failed install produce a
# *successful* build of an image with no test runner in it, and the verifier then reported the
# deliverable's own suite as failing. A build step that cannot fail is not a build step.
RUN if [ -f /tmp/proxy-ca.crt ]; then export PIP_CERT=/tmp/proxy-ca.crt REQUESTS_CA_BUNDLE=/tmp/proxy-ca.crt; fi; \
    pip install --no-cache-dir --disable-pip-version-check \
        pytest==8.3.4 \
        pluggy==1.5.0 \
        iniconfig==2.0.0 \
        packaging==24.2 \
 && python -m pytest --version \
 && rm -f /tmp/proxy-ca.crt

# The subject never runs as root. The container is additionally started with --user, --cap-drop ALL,
# --read-only, --pids-limit, --memory and no network; this line is defence in depth, not the boundary.
USER 65534:65534
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1
