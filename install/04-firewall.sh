#!/usr/bin/env bash
# 방화벽: 사무실 네트워크에서만 접속을 허용하고, 컨테이너는 LLM 중계 포트만 쓸 수 있게 한다.
#   sudo bash install/04-firewall.sh
source "$(dirname "$0")/lib.sh"
need_root

say "ufw 규칙"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow from "$LAN_SUBNET" to any port "$HUB_PORTAL_PORT" proto tcp comment 'mgs portal'
ufw allow from "$LAN_SUBNET" to any port "${HUB_ENV_PORT_START}:${HUB_ENV_PORT_END}" proto tcp comment 'mgs env ports'
ufw allow from "$LAN_SUBNET" to any port "$HUB_LLM_PORT" proto tcp comment 'mgs llm'
ufw allow from "$LAN_SUBNET" to any port 8090 proto tcp comment 'open-webui'
ufw allow from "$LAN_SUBNET" to any app Samba
# 편집·실행 컨테이너 → 메인: LLM 중계 포트만 허용 (나머지 메인 서비스와 다른 컨테이너 접근은 막힘)
ufw allow from "$EDITOR_SUBNET" to any port "$HUB_LLM_PORT" proto tcp comment 'editors -> llm shim'
ufw allow from "$WORKER_SUBNET" to any port "$HUB_LLM_PORT" proto tcp comment 'worker -> llm shim'
ufw --force enable
ufw status verbose

say "완료. 다음: bash install/05-build-runtime.sh"
