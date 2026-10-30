# 인계 문서 — 2026-10-30 사무실 밖 접속 (VPN)

사용자 질문: 사무실 네트워크 밖에서도 메인 GPU 서버를 쓸 수 있는가.
결론: Tailscale VPN 으로만 허용하고, 인터넷 쪽 포트는 하나도 열지 않는다.
설치·사용 절차는 [vpn-tailscale.md](vpn-tailscale.md)에 있다.

## 1. 바뀐 파일

- `install/06-vpn.sh` 를 새로 만들었다.
  - 예: Tailscale 공식 apt 저장소 등록 → 설치 → `tailscaled` 시작까지만 한다.
  - 예: `tailscale up` 은 관리자가 회사 계정으로 직접 로그인해야 하므로 스크립트가 실행하지 않고 명령만 안내한다.
- `install/04-firewall.sh` 가 `VPN_IFACE`(기본 `tailscale0`)에 사무실과 같은 포털·작업공간·LLM 포트를 연다.
  - 예: 80, `HUB_ENV_PORT_START..END`(8101~8130), 11434.
- `.env.example` 에 `VPN_IFACE`, `VPN_ALLOW_SSH`, `VPN_ALLOW_SAMBA` 를 추가했다.
  - 예: 기존 `.env` 에 없으면 스크립트가 `tailscale0`, `false`, `false` 로 동작한다.
- `install/update.sh` 가 `06-vpn.sh` 변경도 "직접 다시 실행할 스크립트"로 알린다.
- `compose.yaml` 은 주석만 바꿨다 (동작 변화 없음).

## 2. 결정과 이유

### 포트 포워딩·공개 터널은 쓰지 않는다

- ComfyUI 커스텀 노드는 임의의 파이썬 코드를 실행한다.
  - 예: 로그인만 되면 Manager 로 노드를 설치하거나 코드가 든 워크플로우를 실행해 메인 GPU 컨테이너 안에서 원하는 코드를 돌릴 수 있다.
  - 예: 실행 공간은 지금 인터넷 접속이 열려 있어(v0.2 후보), 들어온 코드가 밖으로 데이터를 보낼 수도 있다.
- 지금 서버는 HTTP 만 쓴다.
  - 예: 공용 와이파이에서 접속하면 비밀번호와 세션 쿠키가 평문으로 지나간다.
- 포털 로그인에는 시도 횟수 제한이 없다.
  - 예: 인터넷에 열면 비밀번호 무차별 대입에 그대로 노출된다 (`login_fail` 기록만 남는다).
- 공유기 포트 포워딩은 위 세 가지를 인터넷 전체에 드러낸다.
- 공개 터널도 같은 노출이다.
  - 예: Cloudflare Tunnel, ngrok, `tailscale funnel` 은 누구나 주소만 알면 로그인 화면에 닿는다.
  - 예: `tailscale funnel` 은 절대 켜지 않는다 (`tailscale funnel status` 로 확인).
- VPN 은 VPN 에 들어온 기기만 메인 포트를 볼 수 있고, 구간 전체가 WireGuard 로 암호화된다.

### Tailscale 을 고른 이유

- 메인과 작업자 쪽 공유기에 아무 포트도 열 필요가 없다.
- 회사 계정으로 로그인하고 기기를 콘솔에서 바로 끊을 수 있다.
- 회사 정책상 외부 서비스가 안 되면 자체 WireGuard(또는 Headscale)로 바꾼다.
  - 예: `VPN_IFACE=wg0` 으로 바꾸면 방화벽 스크립트는 그대로 쓴다. 이 경우만 공유기에 UDP 포트 하나가 필요하다.
- **회사 네트워크·IT 승인이 먼저 필요하다 (사용자 확인 대기).**

### SSH 는 VPN 에서 기본으로 막는다

- 작업자에게는 메인 터미널 권한을 주지 않는다는 기존 원칙 그대로다.
- VPN 에 붙은 노트북을 잃어버려도 sshd 가 보이지 않게 한다.
- 관리자가 밖에서 점검해야 할 때만 `VPN_ALLOW_SSH=true` 로 열고, Tailscale 정책으로 관리자 기기에만 22번을 허용한다.
- Tailscale SSH(`tailscale up --ssh`)는 쓰지 않는다.
  - 예: ufw 를 거치지 않고 별도 인증으로 22번을 열어 지금의 방화벽 설계를 우회한다.

### 결과 폴더(Samba)는 선택, 기본 꺼짐

- 결과 폴더는 누구나 읽기·쓰기라 노출 범위를 최소로 둔다.
- 영상 결과처럼 큰 파일이 VPN 으로 오가면 느리고, 밖에서는 ComfyUI 화면에서 내려받으면 충분하다.
- 필요하면 `VPN_ALLOW_SAMBA=true` + Tailscale 정책에 445 추가.

### 채팅 화면(Open WebUI, 8090)은 사무실 전용

- Docker 가 여는 포트는 ufw 를 거치지 않아서 `HUB_BIND_IP`(사무실 IP)에만 붙여 두었다.
- Tailscale IP 에도 붙이는 방법은 쓰지 않았다.
  - 예: 부팅 때 Docker 가 `tailscaled` 보다 먼저 컨테이너를 띄우면 그 주소가 아직 없어 Open WebUI 가 시작하지 못한다.
- `0.0.0.0` 으로 바꾸는 방법도 쓰지 않았다.
  - 예: ufw 를 우회하므로 사무실 밖 인터페이스 전부에 열린다.
- 밖에서는 ComfyUI '메인 GPU' 패널과 LLM API(11434, 허브 중계)를 쓴다.
- 포털·패널의 채팅 링크(`http://<요청 주소>:8090/`)는 VPN 에서 누르면 열리지 않는다 (알려진 제약).

## 3. 허브 확인 결과 (코드 기준, 메인 실측 전)

- 허브는 `network_mode: host` 이고 `hub/app/main.py` 의 `_bind()` 가 `0.0.0.0` 으로 듣는다.
  - 예: `tailscale0` 이 허브보다 늦게 생겨도 Tailscale IP 로 접속된다. 막고 여는 것은 ufw 가 정한다.
- 포털·작업공간 링크는 요청의 Host 헤더로 만든다.
  - 예: `portal.py` 의 `_host()`, `proxy.py` 의 `_portal_url()`·`_down_page()`, `panel.py` 의 링크, `mgs_panel.js` 의 `location.hostname`.
  - 예: `HUB_PUBLIC_HOST` 는 Host 헤더가 없을 때만 쓴다.
- 코드에 `192.168.10.29` 를 박아 둔 곳은 없다 (문서에만 있다).
- 로그인 뒤 돌아갈 주소 검사(`_safe_next`)도 요청 Host 와 비교하므로 VPN 주소에서 그대로 동작한다.
- 세션 쿠키는 도메인 없이 설정되어 주소별로 따로다.
  - 예: 사무실 IP 와 VPN 주소는 로그인을 각각 한 번씩 한다.
- 허브는 IPv4 로만 듣는다.
  - 예: MagicDNS 이름이 IPv6 주소를 먼저 주어도 브라우저가 IPv4 로 다시 붙는다. 이름이 느리면 `100.x.y.z` 로 연다.

## 4. HTTPS (`tailscale serve` / `tailscale cert`) 판단

- 지금은 하지 않는다.
- VPN 구간은 이미 WireGuard 로 암호화되어 HTTPS 를 더해도 도청 방어는 거의 늘지 않는다.
- 허브는 포털과 작업공간을 포트별로 나누고 링크를 `http://` 로 만든다.
  - 예: HTTPS 를 붙이려면 포트 31개를 각각 `tailscale serve` 로 연결하고, 링크 만드는 코드를 요청 스킴 기준으로 고쳐야 한다.
- 다시 검토할 조건은 아래와 같다.
  - 예: 회사 감사에서 사무실 LAN 구간 평문 HTTP 가 문제 될 때.
  - 예: 브라우저가 HTTPS 에서만 허용하는 기능(클립보드 API 등)이 꼭 필요해질 때.
  - 예: 이때는 v0.2 에서 링크 스킴을 요청 기준으로 바꾸고 `tailscale serve` 로 HTTPS 를 붙이는 방안을 본다.
- 어떤 경우에도 `tailscale funnel`(인터넷 공개)은 쓰지 않는다.

## 5. 다음 할 일

- 회사 네트워크·IT 승인 받기 (Tailscale 사용 가능 여부, 요금제의 사용자 수).
- 메인에서 `sudo bash install/04-firewall.sh` → `sudo bash install/06-vpn.sh` → `sudo tailscale up --hostname=main-gpu --accept-dns=false`.
- 관리 콘솔에서 메인 키 만료 끄기, `tag:mgs-main` 태그, 접근 정책 저장.
- [vpn-tailscale.md](vpn-tailscale.md) 의 확인표를 사무실 밖(휴대폰 핫스팟)에서 실측한다.
  - 예: 특히 22번과 8090 이 `False` 인지 본다. Tailscale 이 만드는 iptables 규칙과 ufw 의 실제 순서는 메인에서 확인해야 한다.
- v0.2 후보: 로그인 시도 횟수 제한, Open WebUI 를 허브 뒤로 옮겨 VPN 에서도 쓰기.
