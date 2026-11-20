#!/bin/bash
# Gera o pacote .deb do bootbell em dist/.
#
# Uso: ./build-deb.sh            (saída em ./dist)
#      OUT_DIR=/tmp ./build-deb.sh
#
# Requer somente dpkg-deb e gzip. O pacote é compactado com xz para que
# versões antigas do dpkg (Debian 10, Ubuntu 18.04) consigam instalá-lo.
set -euo pipefail
umask 022

cd "$(dirname "$(readlink -f "$0")")"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/bootbell.py)
if [ -z "$VERSION" ]; then
    echo "erro: não encontrei __version__ em src/bootbell.py" >&2
    exit 1
fi
CHANGELOG_VERSION=$(sed -n '1s/^bootbell (\([^)]*\)).*/\1/p' packaging/debian/changelog)
if [ "$VERSION" != "$CHANGELOG_VERSION" ]; then
    echo "erro: versão $VERSION em src/bootbell.py difere de $CHANGELOG_VERSION em packaging/debian/changelog" >&2
    exit 1
fi

OUT_DIR=${OUT_DIR:-dist}
mkdir -p "$OUT_DIR"

# Datas fixas deixam o build reproduzível.
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
PKG="$BUILD/bootbell"
mkdir -m 0755 "$PKG"

install -Dm0755 src/bootbell.py           "$PKG/usr/bin/bootbell"
install -Dm0644 packaging/bootbell.service "$PKG/usr/lib/systemd/system/bootbell.service"
install -Dm0640 packaging/bootbell.conf    "$PKG/etc/bootbell/bootbell.conf"

install -d "$PKG/usr/share/man/man1" "$PKG/usr/share/doc/bootbell" "$PKG/usr/share/lintian/overrides"
gzip -9n < docs/bootbell.1               > "$PKG/usr/share/man/man1/bootbell.1.gz"
gzip -9n < packaging/debian/changelog    > "$PKG/usr/share/doc/bootbell/changelog.gz"
install -m0644 packaging/debian/copyright  "$PKG/usr/share/doc/bootbell/copyright"
install -m0644 README.md                   "$PKG/usr/share/doc/bootbell/README.md"
install -m0644 packaging/debian/lintian-overrides "$PKG/usr/share/lintian/overrides/bootbell"
chmod 0644 "$PKG"/usr/share/man/man1/* "$PKG"/usr/share/doc/bootbell/*

install -d "$PKG/DEBIAN"
INSTALLED_SIZE=$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@INSTALLED_SIZE@/$INSTALLED_SIZE/" \
    packaging/debian/control.in > "$PKG/DEBIAN/control"
install -m0644 packaging/debian/conffiles "$PKG/DEBIAN/conffiles"
install -m0755 packaging/debian/postinst packaging/debian/prerm packaging/debian/postrm "$PKG/DEBIAN/"
(cd "$PKG" && find . -path ./DEBIAN -prune -o -path ./etc -prune -o -type f -printf '%P\0' \
    | LC_ALL=C sort -z | xargs -0 md5sum > DEBIAN/md5sums)
chmod 0644 "$PKG/DEBIAN/md5sums"

find "$PKG" -exec touch --no-dereference --date="@$SOURCE_DATE_EPOCH" {} +

DEB="$OUT_DIR/bootbell_${VERSION}_all.deb"
dpkg-deb -Zxz --root-owner-group --build "$PKG" "$DEB" >/dev/null
echo "$DEB"
