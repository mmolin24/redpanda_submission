#!/bin/sh
set -eu

case "${MODEL_MODE:-fake}" in
  fake)
    ;;
  openai)
    if [ -z "${OPENAI_API_KEY:-}" ]; then
      echo "MODEL_MODE=openai requires OPENAI_API_KEY; live ingress will not start." >&2
      exit 1
    fi
    ;;
  *)
    echo "MODEL_MODE must be fake or openai; live ingress will not start." >&2
    exit 1
    ;;
esac
