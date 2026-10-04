#!/usr/bin/env bash
# Installs what a thread browser's live view needs in a sandbox image: Xorg with the dummy
# video driver, openbox, GStreamer, and the pinned neko server (https://github.com/m1k1o/neko).
# Run it as root from a workspace's setup script. It is safe to re-run.
set -euo pipefail

NEKO_IMAGE="${NEKO_IMAGE:-ghcr.io/m1k1o/neko/base@sha256:33c3406ab1699294949d9188999e340950fecdb9352cb65d84706d69d297763c}"
CRANE_VERSION="${CRANE_VERSION:-v0.20.3}"
NEKO_ROOT="${NEKO_ROOT:-/opt/neko}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl python3 xclip \
  xserver-xorg-core xserver-xorg-video-dummy openbox \
  libx11-6 libxrandr2 libxtst6 libxcvt0 \
  libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly

case "$(uname -m)" in
  x86_64) crane_arch=x86_64 ;;
  aarch64 | arm64) crane_arch=arm64 ;;
  *) echo "Unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
curl -fsSL "https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_Linux_${crane_arch}.tar.gz" \
  | tar -xz -C "$workdir" crane

mkdir -p "$NEKO_ROOT/bin"
"$workdir/crane" export "$NEKO_IMAGE" - | tar -x -C "$workdir" usr/bin/neko
install -m 0755 "$workdir/usr/bin/neko" "$NEKO_ROOT/bin/neko"

if ldd "$NEKO_ROOT/bin/neko" | grep -q 'not found'; then
  ldd "$NEKO_ROOT/bin/neko" | grep 'not found' >&2
  echo "neko is missing shared libraries" >&2
  exit 1
fi
echo "neko installed at $NEKO_ROOT/bin/neko"
