#!/usr/bin/env bash
# Installs what a thread browser's live view needs in a sandbox image: Xorg with the dummy
# video driver, openbox, xrandr, ffmpeg (x11grab and libx264), and the X client libraries
# the display helper loads. Run it as root from a workspace's setup script. It is safe to
# re-run. Without these the browser still runs headless; sessions install them on first use.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  xserver-xorg-core xserver-xorg-video-dummy openbox x11-xserver-utils \
  ffmpeg python3 libx11-6 libxtst6

ffmpeg -hide_banner -devices 2>/dev/null | grep -q x11grab
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264
echo "browser live view dependencies installed"
