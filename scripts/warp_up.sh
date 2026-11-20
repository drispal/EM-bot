#!/usr/bin/env bash
# Donne un accès IPv6 au runner GitHub (qui n'en a pas) via un tunnel WireGuard Cloudflare WARP.
# Nécessaire : les hôtes Turnstile (brunhild.challenges.cloudflare.com) n'ont que des adresses IPv6.
set -euo pipefail
sudo apt-get install -y -qq wireguard-tools >/dev/null
tmp=$(mktemp -d); cd "$tmp"
gh release download -R ViRb3/wgcf -p '*linux_amd64' -O wgcf --clobber
chmod +x wgcf
./wgcf register --accept-tos >/dev/null
./wgcf generate >/dev/null
sed -i '/^DNS/d' wgcf-profile.conf
sudo install -m 600 wgcf-profile.conf /etc/wireguard/wgcf.conf
sudo wg-quick up wgcf
echo "IPv6 : $(curl -6 -s -m 8 https://ifconfig.co/ip 2>&1 || echo KO) | IPv4 : $(curl -4 -s -m 8 https://ifconfig.co/ip 2>&1 || echo KO)"
echo "brunhild curl -6 : $(curl -6 -s -o /dev/null -m 8 -w '%{http_code}' https://brunhild.challenges.cloudflare.com/ 2>&1 || true)"
