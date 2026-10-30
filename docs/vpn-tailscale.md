# 사무실 밖에서 메인 GPU 서버 쓰기 (Tailscale VPN)

사무실 밖 작업자는 회사 VPN(Tailscale)에 들어온 뒤에만 메인에 접속합니다.
인터넷 쪽으로는 메인의 포트를 하나도 열지 않습니다.
보안 판단과 이유는 [handover-2026-10-30-vpn.md](handover-2026-10-30-vpn.md)에 있습니다.

## 1. 구조

- 작업자 노트북과 메인이 같은 Tailscale 네트워크(tailnet)에 들어가면 메인에 `100.x.y.z` 주소가 하나 더 생깁니다.
  - 예: 사무실에서는 `http://192.168.10.29/`, 밖에서는 `http://100.101.102.103/` 또는 `http://main-gpu/`.
- 메인 방화벽(ufw)은 사무실 대역과 `tailscale0` 인터페이스에서만 포털·작업공간·LLM 포트를 엽니다.
  - 예: 80, 8101~8130, 11434.
- 공유기 포트 포워딩과 공개 터널은 쓰지 않으므로 인터넷에서는 메인의 어떤 포트도 보이지 않습니다.
- 허브는 모든 주소(`0.0.0.0`)에서 듣고 링크를 요청 주소 기준으로 만들므로 VPN 주소로 들어와도 작업공간 링크가 그대로 동작합니다.
  - 예: `http://main-gpu/` 로 로그인하면 내 ComfyUI 는 `http://main-gpu:8101/` 로 열립니다.

| 항목 | 포트 | 사무실 | VPN |
|---|---|---|---|
| 포털·로그인 | 80 | 열림 | 열림 |
| 작업공간 ComfyUI | 8101~8130 | 열림 | 열림 |
| LLM API | 11434 | 열림 | 열림 |
| SSH | 22 | 열림 | 기본 막힘 (`VPN_ALLOW_SSH=true` 일 때 관리자 기기만) |
| 결과 폴더(Samba) | 445 | 열림 | 기본 막힘 (`VPN_ALLOW_SAMBA=true` 일 때) |
| 채팅(Open WebUI) | 8090 | 열림 | 안 열림 (사무실 전용) |

## 2. 먼저 정할 것 (회사 승인)

- Tailscale 은 외부 회사의 조정 서버를 쓰므로 설치 전에 회사 네트워크·IT 담당의 승인을 받습니다.
  - 예: 화면과 결과 파일은 기기끼리 암호화(WireGuard)되어 오가고, Tailscale 서버는 기기 목록과 공개 키만 다룹니다.
  - 예: 기기끼리 직접 연결이 안 되면 Tailscale 중계 서버(DERP)를 거치지만, 이때도 내용은 암호화된 채로 지나갑니다.
- 외부 서비스가 금지되면 같은 구조를 자체 WireGuard 서버(또는 Headscale)로 바꿉니다.
  - 예: `.env` 의 `VPN_IFACE=wg0` 으로 바꾸면 `04-firewall.sh` 규칙은 그대로 씁니다.
  - 예: 자체 WireGuard 는 공유기에 UDP 포트 하나(기본 51820)를 열어야 하지만, 키가 없는 접속에는 응답하지 않습니다.
- tailnet 은 회사 계정(Google Workspace 또는 Microsoft)으로 만들고 작업자를 초대합니다.
- 요금제의 사용자 수·기기 수 제한을 가입 전에 확인합니다.

## 3. 메인 설치 (관리자, `sudo` 는 직접 실행)

- `.env` 에 VPN 항목이 있는지 확인합니다.
  - 예: `VPN_IFACE=tailscale0`, `VPN_ALLOW_SSH=false`, `VPN_ALLOW_SAMBA=false` (없으면 이 기본값으로 동작하고, `update.sh` 가 새 항목을 알려 줍니다).
- 방화벽 규칙을 다시 적용합니다.

```bash
cd /opt/mgs-src && sudo bash install/04-firewall.sh
```

- Tailscale 을 설치합니다.

```bash
sudo bash install/06-vpn.sh
```

- 메인을 tailnet 에 로그인시킵니다.
  - 예: 나오는 주소를 브라우저로 열어 회사 관리자 계정으로 승인합니다.

```bash
sudo tailscale up --hostname=main-gpu --accept-dns=false
tailscale ip -4
```

- `--ssh` 옵션은 쓰지 않습니다.
  - 예: Tailscale SSH 는 ufw 를 거치지 않고 별도 인증으로 22번을 열기 때문입니다.
- 관리 콘솔(Machines)에서 메인의 키 만료를 끕니다.
  - 예: 메인 행의 `...` → `Disable key expiry`. 끄지 않으면 기본 180일 뒤 메인이 VPN 에서 빠집니다.
- 관리 콘솔에서 메인에 태그 `tag:mgs-main` 을 붙입니다.
  - 예: 메인 행의 `...` → `Edit ACL tags` (아래 정책의 `tagOwners` 를 먼저 저장해야 고를 수 있습니다).
- 관리 콘솔(Access controls)의 정책을 아래처럼 바꿉니다.

```json
{
  "tagOwners": { "tag:mgs-main": ["autogroup:admin"] },
  "acls": [
    { "action": "accept", "src": ["autogroup:member"], "dst": ["tag:mgs-main:80,8101-8130,11434"] }
  ]
}
```

- 이 정책은 ufw 와 별도로 한 번 더 막는 장치입니다.
  - 예: 작업자 기기는 메인의 80·8101~8130·11434 에만 닿고, 작업자 노트북끼리는 서로 접속할 수 없습니다.
- `VPN_ALLOW_SSH=true` 로 바꿨다면 관리자 기기에만 22번을 허용하는 줄을 추가합니다.
  - 예: `{ "action": "accept", "src": ["autogroup:admin"], "dst": ["tag:mgs-main:22"] }`
- `VPN_ALLOW_SAMBA=true` 로 바꿨다면 작업자 줄의 포트에 445 를 추가합니다.
  - 예: `"tag:mgs-main:80,445,8101-8130,11434"`
- 설정을 바꾼 뒤에는 `04-firewall.sh` 를 다시 실행합니다.
  - 예: `false` 로 되돌리면 그 규칙은 지워집니다.

### 확인

| 확인 | 방법 | 기대 결과 |
|---|---|---|
| 방화벽 규칙 | `sudo ufw status verbose` | `on tailscale0` 줄에 80, 8101:8130, 11434 만 있음 |
| 공개 노출 없음 | `tailscale serve status`, `tailscale funnel status` | 설정 없음 |
| 밖에서 포털 | 휴대폰 핫스팟에 붙은 노트북(VPN 켬)에서 `http://main-gpu/` | 로그인 화면 |
| 밖에서 작업공간 | 로그인 후 내 ComfyUI | `:8101` 등이 같은 이름으로 열림 |
| SSH 막힘 | PowerShell `Test-NetConnection main-gpu -Port 22` | `TcpTestSucceeded : False` |
| 채팅 막힘 | PowerShell `Test-NetConnection main-gpu -Port 8090` | `TcpTestSucceeded : False` |
| VPN 끄면 안 됨 | 노트북에서 Tailscale 끄고 `http://main-gpu/` | 열리지 않음 |

## 4. 작업자 노트북 안내

- Tailscale 을 공식 사이트에서만 받아 설치합니다.
  - 예: https://tailscale.com/download → Windows.
- 관리자가 보낸 초대 메일의 링크로 회사 계정에 로그인합니다.
  - 예: 작업 표시줄의 Tailscale 아이콘 → `Log in` → 회사 계정 선택.
- 관리자가 기기 승인을 켜 두었다면 승인될 때까지 기다립니다.
- 브라우저에서 메인 주소를 엽니다.
  - 예: `http://main-gpu/` (이름이 안 열리면 관리자가 알려 준 `http://100.x.y.z/`).
  - 예: 사무실 주소(`http://192.168.10.29/`) 북마크는 밖에서 열리지 않으니 VPN 주소 북마크를 따로 만듭니다.
- 로그인하면 사무실과 똑같이 내 ComfyUI 가 열립니다.
  - 예: 사무실 주소와 VPN 주소는 로그인이 따로라서 처음 한 번은 다시 로그인합니다.
- 결과 파일은 기본으로 ComfyUI 화면에서 내려받습니다.
  - 예: 결과 이미지를 오른쪽 클릭 → 저장.
- 관리자가 결과 폴더(Samba)를 VPN 에 열어 두었다면 결과 폴더를 연결할 수 있습니다.
  - 예: `net use R: \\100.x.y.z\results /user:alice /persistent:yes`
- 채팅(Open WebUI, 8090) 링크는 밖에서 열리지 않습니다.
  - 예: LLM 은 ComfyUI 의 '메인 GPU' 패널이나 API 키로 `http://main-gpu:11434` 를 씁니다.
- 노트북을 잃어버리거나 그만 쓰게 되면 바로 관리자에게 알립니다.
  - 예: 관리자는 관리 콘솔에서 그 기기를 지우고, 메인에서 `sudo docker compose exec -T hub python -m app.cli set-password alice --password-stdin` 으로 비밀번호를 바꿉니다.
- 사무실 안에서는 VPN 을 켜 두어도 되고 꺼도 됩니다.

### 안 열릴 때

- Tailscale 아이콘이 `Connected` 인지 봅니다.
- 이름 대신 `100.x.y.z` 주소로 열어 봅니다.
  - 예: 회사 DNS 나 다른 VPN 이 MagicDNS 이름을 가로채는 경우가 있습니다.
- 명령 프롬프트에서 메인까지 연결되는지 봅니다.
  - 예: `tailscale ping main-gpu` 가 `pong` 이면 VPN 은 정상이고, 그래도 안 열리면 관리자에게 알립니다.
