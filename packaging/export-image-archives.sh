#!/bin/sh
# Export the offline image archives of IMAGE:VERSION straight from the registry, one
# single-architecture docker-archive (.tar.gz, readable by `docker load -i`) per platform.
# Uses skopeo instead of `docker pull` + `docker save`: with the containerd image store the
# daemon reports an empty architecture for non-native pulls, which broke the v0.2.0 release.
# Usage: export-image-archives.sh IMAGE VERSION OUTDIR
set -eu
IMAGE=$1
VERSION=$2
OUT=$3
mkdir -p "$OUT"
for platform in linux/amd64 linux/arm64; do
  name=$(echo "${platform#linux/}" | tr / -)
  arch=${platform#linux/}
  variant=
  case $arch in */*) variant=${arch#*/}; arch=${arch%%/*} ;; esac
  tar="$OUT/sme-ocpp-sim-$VERSION-linux-$name.tar"
  if [ -n "$variant" ]; then
    skopeo copy --override-os linux --override-arch "$arch" --override-variant "$variant" \
      "docker://$IMAGE:$VERSION" "docker-archive:$tar:$IMAGE:$VERSION"
  else
    skopeo copy --override-os linux --override-arch "$arch" \
      "docker://$IMAGE:$VERSION" "docker-archive:$tar:$IMAGE:$VERSION"
  fi
  # Check the archive really holds the requested architecture.
  python3 - "$tar" "$arch" "$variant" <<'EOF'
import json, sys, tarfile
path, arch, variant = sys.argv[1:]
with tarfile.open(path) as t:
    manifest = json.load(t.extractfile("manifest.json"))
    config = json.load(t.extractfile(manifest[0]["Config"]))
got = (config.get("architecture"), config.get("variant") or "")
if got[0] != arch or (variant and got[1] != variant):
    sys.exit(f"{path}: got {got}, expected {(arch, variant)}")
print(f"{path}: {got[0]} {got[1]}".rstrip())
EOF
  gzip -9 "$tar"
done
ls -l "$OUT"
