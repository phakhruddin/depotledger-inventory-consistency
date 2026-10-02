#!/bin/sh
# Builds both supplied images. Invoked by runtime.sh in the agent environment
# and by the verifier runtime, so both sides get byte-identical images.
set -eu
APPLICATION_DIR="${APPLICATION_DIR:-/application}"
docker build -q -f "$APPLICATION_DIR/Dockerfile.api" -t depotledger/api:1.0.0 "$APPLICATION_DIR"
docker build -q -f "$APPLICATION_DIR/Dockerfile.snapshotter" -t depotledger/snapshotter:1.0.0 "$APPLICATION_DIR"
