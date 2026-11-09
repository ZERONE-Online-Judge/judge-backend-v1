#!/bin/sh
set -eu
# Versions from https://icpc.global/worldfinals/programming-environment
case "$(uname -m)" in
  x86_64) pypy_arch=linux64; pypy_sha=f062be307200bde434817e1620cebc13f563d6ab25309442c5f4d0f0d68f0912 ;;
  aarch64) pypy_arch=aarch64; pypy_sha=03e35fcba290454bb0ccf7ee57fb42d1e63108d10d593776a382c0a2fe355de0 ;;
  *) echo 'Unsupported PyPy architecture' >&2; exit 1 ;;
esac
curl -fLS --retry 3 "https://downloads.python.org/pypy/pypy3.9-v7.3.15-${pypy_arch}.tar.bz2" -o /tmp/pypy.tar.bz2
printf '%s  %s\n' "$pypy_sha" /tmp/pypy.tar.bz2 | sha256sum -c -
mkdir -p /usr/local/lib/pypy
 tar -xjf /tmp/pypy.tar.bz2 -C /usr/local/lib/pypy --strip-components=1
ln -s /usr/local/lib/pypy/bin/pypy3 /usr/local/bin/pypy3
# Request a fresh release redirect; the archive remains pinned by SHA256 below.
curl -fLS --retry 3 "https://github.com/JetBrains/kotlin/releases/download/v1.9.24/kotlin-compiler-1.9.24.zip?zoj_download=$(date +%s)" -o /tmp/kotlin.zip
printf '%s  %s\n' eb7b68e01029fa67bc8d060ee54c12018f2c60ddc438cf21db14517229aa693b /tmp/kotlin.zip | sha256sum -c -
unzip -q /tmp/kotlin.zip -d /usr/local/lib
mv /usr/local/lib/kotlinc /usr/local/lib/kotlin
cat > /usr/local/bin/kotlinc <<'EOF'
#!/bin/sh
export JAVA_HOME=/usr/local/lib/icpc-jdk
exec /usr/local/lib/kotlin/bin/kotlinc "$@"
EOF
chmod 755 /usr/local/bin/kotlinc
rm /tmp/pypy.tar.bz2 /tmp/kotlin.zip
/usr/local/bin/pypy3 --version
/usr/local/bin/kotlinc -version
