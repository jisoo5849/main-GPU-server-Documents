#!/usr/bin/env bash
# Samba 공유(\\main\results, \\main\model-inbox)를 설정한다.
#   sudo bash install/03-samba.sh
source "$(dirname "$0")/lib.sh"
need_root

say "smb.conf 설치"
[[ -f /etc/samba/smb.conf && ! -f /etc/samba/smb.conf.before-mgs ]] && cp /etc/samba/smb.conf /etc/samba/smb.conf.before-mgs
sed -e "s#@MGS_RESULTS@#${MGS_RESULTS}#g" \
    -e "s#@MGS_MODELS@#${MGS_MODELS}#g" \
    -e "s#@MGS_GROUP@#${MGS_GROUP}#g" \
    "$REPO_DIR/install/smb.conf" > /etc/samba/smb.conf
testparm -s >/dev/null

say "휴지통 30일 자동 정리"
cat > /etc/cron.daily/mgs-recycle <<EOF
#!/bin/sh
find "${MGS_RESULTS}/.recycle" -type f -mtime +30 -delete 2>/dev/null
find "${MGS_RESULTS}/.recycle" -mindepth 1 -type d -empty -delete 2>/dev/null
exit 0
EOF
chmod 755 /etc/cron.daily/mgs-recycle

systemctl enable --now smbd
systemctl restart smbd

say "완료. 사용자는 install/add-user.sh 로 추가합니다. 다음: sudo bash install/04-firewall.sh"
