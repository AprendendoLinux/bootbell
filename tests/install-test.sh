#!/bin/sh
# Instala o .deb dentro de um container Debian/Ubuntu e roda os testes contra
# o programa instalado. Usado pelo CI e para testes locais:
#
#   docker run --rm -v "$PWD":/src -w /src ubuntu:18.04 sh tests/install-test.sh
set -eu

. /etc/os-release
echo "==> $PRETTY_NAME"

# Debian 10 e 11 (fim do LTS) saíram do espelho principal e foram para o
# arquivo histórico.
if [ "$ID" = "debian" ] && { [ "$VERSION_ID" = "10" ] || [ "$VERSION_ID" = "11" ]; }; then
    sed -i -e 's|deb.debian.org|archive.debian.org|g' \
           -e 's|security.debian.org|archive.debian.org|g' \
           -e '/-updates/d' /etc/apt/sources.list
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ./dist/bootbell_*_all.deb >/dev/null

echo "==> instalado: $(bootbell --version) com $(python3 --version)"
id bootbell
perms=$(stat -c '%U:%G %a' /etc/bootbell/bootbell.conf)
[ "$perms" = "root:bootbell 640" ] || { echo "permissões erradas: $perms"; exit 1; }
test -L /etc/systemd/system/multi-user.target.wants/bootbell.service
# Sem token configurado o check deve acusar o problema.
if bootbell check >/dev/null; then echo "check deveria falhar sem token"; exit 1; fi
# (as imagens Docker "minimizadas" do Ubuntu não instalam man pages)
dpkg -L bootbell | grep -q /usr/share/man/man1/bootbell.1.gz

BOOTBELL_MODULE=/usr/bin/bootbell python3 -m unittest discover -s tests

apt-get remove -y -qq bootbell >/dev/null
test -f /etc/bootbell/bootbell.conf
apt-get purge -y -qq bootbell >/dev/null
test ! -e /etc/bootbell
test ! -e /etc/systemd/system/multi-user.target.wants/bootbell.service
test -e /usr/lib/systemd/system/bootbell.service && { echo "unit não foi removida"; exit 1; }
test ! -L /lib/systemd/system/bootbell.service
echo "==> OK: $PRETTY_NAME"
