#!/usr/bin/env bash
# 방화벽: 사무실 네트워크와 회사 VPN(Tailscale)에서만 접속을 허용하고, 컨테이너는 LLM 중계 포트만 쓸 수 있게 한다.
# 인터넷 쪽으로는 아무 포트도 열지 않는다 (공유기 포트 포워딩·공개 터널 금지, docs/vpn-tailscale.md).
#   sudo bash install/04-firewall.sh
source "$(dirname "$0")/lib.sh"
need_root

VPN_IFACE="${VPN_IFACE-tailscale0}"

say "ufw 규칙"
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$LAN_SUBNET" to any port 22 proto tcp comment 'ssh'
ufw allow from "$LAN_SUBNET" to any port "$HUB_PORTAL_PORT" proto tcp comment 'mgs portal'
ufw allow from "$LAN_SUBNET" to any port "${HUB_ENV_PORT_START}:${HUB_ENV_PORT_END}" proto tcp comment 'mgs env ports'
ufw allow from "$LAN_SUBNET" to any port "$HUB_LLM_PORT" proto tcp comment 'mgs llm'
ufw allow from "$LAN_SUBNET" to any app Samba
# 편집·실행 컨테이너 → 메인: LLM 중계 포트만 허용 (나머지 메인 서비스와 다른 컨테이너 접근은 막힘)
ufw allow from "$EDITOR_SUBNET" to any port "$HUB_LLM_PORT" proto tcp comment 'editors -> llm shim'
ufw allow from "$WORKER_SUBNET" to any port "$HUB_LLM_PORT" proto tcp comment 'worker -> llm shim'
# Open WebUI(기본 docker 브리지) → 메인 LLM 중계
ufw allow in on docker0 to any port "$HUB_LLM_PORT" proto tcp comment 'open-webui -> llm shim'
# 참고: Docker 가 직접 여는 포트(8090)는 ufw 를 거치지 않는다. compose 의 HUB_BIND_IP 로 사무실 쪽 IP 에만 붙인다.
#       그래서 Open WebUI(8090)는 VPN 에서 열리지 않는다 (사무실 전용).

# 사무실 밖 작업자: VPN 인터페이스로 들어온 접속에 사무실과 같은 포털·작업공간·LLM 포트를 연다.
# 인터페이스는 VPN 안에서만 존재하므로 인터넷 쪽 포트는 그대로 닫혀 있다. 06-vpn.sh 보다 먼저 실행해도 된다.
vpn_rule() {  # vpn_rule true|false '설명' <ufw 규칙...>: 켜면 추가, 끄면 (있을 때만) 지운다
  local on=$1 note=$2; shift 2
  if [[ "$on" == true ]]; then
    ufw allow in on "$VPN_IFACE" "$@" comment "$note"
  else
    ufw delete allow in on "$VPN_IFACE" "$@" >/dev/null 2>&1 || true
  fi
}
if [[ -n "$VPN_IFACE" ]]; then
  say "VPN 규칙 ($VPN_IFACE)"
  vpn_rule true 'mgs portal (vpn)' to any port "$HUB_PORTAL_PORT" proto tcp
  vpn_rule true 'mgs env ports (vpn)' to any port "${HUB_ENV_PORT_START}:${HUB_ENV_PORT_END}" proto tcp
  vpn_rule true 'mgs llm (vpn)' to any port "$HUB_LLM_PORT" proto tcp
  # SSH 는 기본으로 막는다: 작업자에게는 메인 터미널 권한을 주지 않고, VPN 에 붙은 노트북을 잃어버려도 sshd 가 보이지 않게.
  # 관리자가 밖에서 점검해야 할 때만 VPN_ALLOW_SSH=true 로 열고, Tailscale 접근 정책으로 관리자 기기에만 허용한다.
  vpn_rule "${VPN_ALLOW_SSH:-false}" 'ssh (vpn)' to any port 22 proto tcp
  # 결과 폴더(Samba)는 선택: 대용량 파일이 VPN 으로 오가므로 필요할 때만 VPN_ALLOW_SAMBA=true
  vpn_rule "${VPN_ALLOW_SAMBA:-false}" 'samba (vpn)' to any app Samba
else
  warn "VPN_IFACE 가 비어 있어 VPN 규칙을 만들지 않습니다 (이미 만든 규칙은 남아 있습니다: sudo ufw status numbered)."
fi

ufw --force enable
ufw status verbose

say "완료. 다음: bash install/05-build-runtime.sh  (VPN 을 쓰면 sudo bash install/06-vpn.sh)"
