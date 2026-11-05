#!/usr/bin/env bash
# Build a test template: an echo application (ioctl-echo-loop from the emulator's
# rootfs-tools) stopped at its first rx-accepted yield, ready for inputs.
#
#   scripts/make-echo-template.sh <linux.bin> <rootfs-tools.ext2> <out-dir> [image]
#
# The kernel and rootfs must match the emulator version of the image.
set -euo pipefail
kernel=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
rootfs_src=$2
out=$3
image=${4:-cartesi/machine-emulator:0.22.0}

mkdir -p "$(dirname "$out")"
outdir=$(cd "$(dirname "$out")" && pwd)
name=$(basename "$out")
rm -rf "$outdir/$name"
cp "$rootfs_src" "$outdir/$name.rootfs.ext2"   # the machine writes to its flash drive

docker run --rm \
  -v "$kernel:/img/linux.bin:ro" \
  -v "$outdir:/out" \
  --user "$(id -u):$(id -g)" \
  "$image" cartesi-machine \
  --ram-image=/img/linux.bin \
  --flash-drive=label:root,data_filename:/out/$name.rootfs.ext2 \
  --assert-rolling-template \
  --store=/out/$name \
  -- "ioctl-echo-loop --vouchers=1 --notices=1 --reports=1 --verbose=1"

rm -f "$outdir/$name.rootfs.ext2"
echo "template stored in $outdir/$name"
