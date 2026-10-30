#!/usr/bin/env bash
# 사무실 밖 작업자용 VPN(Tailscale)을 메인에 설치한다. 인터넷 쪽으로는 포트를 하나도 열지 않는다.
# 로그인(tailscale up)은 관리자가 직접 한다 (회사 계정으로 브라우저 인증). 이 스크립트는 설치와 서비스 시작까지만 한다.
#   sudo bash install/06-vpn.sh
# 방화벽 규칙(tailscale0 에 포털·작업공간·LLM 포트 허용)은 04-firewall.sh 가 만든다.
source "$(dirname "$0")/lib.sh"
need_root

VPN_IFACE="${VPN_IFACE-tailscale0}"

say "Tailscale (pkgs.tailscale.com 저장소)"
if ! command -v tailscale >/dev/null; then
  . /etc/os-release
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update
  apt-get install -y tailscale
fi
systemctl enable --now tailscaled
tailscale version | head -1

say "방화벽 확인"
if [[ -z "$VPN_IFACE" ]]; then
  warn ".env 의 VPN_IFACE 가 비어 있어 04-firewall.sh 가 VPN 규칙을 만들지 않습니다."
elif ! ufw status | grep -q "on ${VPN_IFACE}"; then
  warn "${VPN_IFACE} 허용 규칙이 없습니다. 이어서 실행하세요:  sudo bash install/04-firewall.sh"
else
  ufw status | grep "on ${VPN_IFACE}" | sed 's/^/  /'
fi

say "로그인 상태"
if ip4=$(tailscale ip -4 2>/dev/null) && [[ -n "$ip4" ]]; then
  echo "  이미 로그인되어 있습니다. 메인 VPN 주소: ${ip4}"
  echo "  작업자 주소: http://${ip4}/  (MagicDNS 이름은 tailscale status 첫 줄)"
else
  # --ssh 는 쓰지 않는다: Tailscale SSH 는 ufw 를 거치지 않고 별도 인증으로 22 번을 연다.
  # --accept-dns=false: 메인은 다른 VPN 기기 이름을 찾을 일이 없으므로 메인 DNS 설정을 건드리지 않는다.
  cat <<'EOF'
  아직 로그인하지 않았습니다. 관리자가 직접 실행하고, 나오는 주소를 브라우저로 열어 회사 계정으로 승인하세요.

    sudo tailscale up --hostname=main-gpu --accept-dns=false

  그다음 확인:
    tailscale ip -4          # 100.x.y.z (작업자가 여는 주소)
    tailscale status         # 첫 줄의 이름이 MagicDNS 이름 (예: main-gpu)
EOF
fi

say "완료. 관리 콘솔에서 메인의 키 만료를 끄고, 접근 정책(ACL)을 docs/vpn-tailscale.md 대로 넣으세요."
