#!/usr/bin/env bash
# 저장소 폴더, 사용자 그룹, 권한을 만든다.
#   sudo bash install/02-storage.sh                  # MGS_ENVS 가 이미 btrfs 로 마운트된 경우
#   sudo bash install/02-storage.sh --format /dev/sdX1   # 빈 파티션을 btrfs 로 포맷해 MGS_ENVS 에 연결 (파티션 내용 삭제!)
source "$(dirname "$0")/lib.sh"
need_root "$@"

FORMAT_DEV=""
if [[ "${1:-}" == "--format" ]]; then
  FORMAT_DEV="${2:?포맷할 장치를 지정하세요. 예: --format /dev/nvme1n1p1}"
fi

say "사용자 그룹 ${MGS_GROUP} (gid ${MGS_GID})"
getent group "$MGS_GROUP" >/dev/null || groupadd -g "$MGS_GID" "$MGS_GROUP"

say "환경 저장소 ${MGS_ENVS} (btrfs)"
mkdir -p "$MGS_ENVS"
if [[ -n "$FORMAT_DEV" ]]; then
  [[ -b "$FORMAT_DEV" ]] || die "$FORMAT_DEV 는 블록 장치가 아닙니다."
  findmnt -n "$FORMAT_DEV" >/dev/null && die "$FORMAT_DEV 가 이미 마운트되어 있습니다."
  warn "$FORMAT_DEV 의 모든 데이터가 지워집니다. 계속하려면 장치 이름을 그대로 입력하세요:"
  read -r answer
  [[ "$answer" == "$FORMAT_DEV" ]] || die "취소했습니다."
  mkfs.btrfs -f -L mgs-envs "$FORMAT_DEV"
  uuid=$(blkid -s UUID -o value "$FORMAT_DEV")
  # noatime: 파일을 읽기만 해도 바뀌는 접근 시각 기록을 끈다(환경 변경 감지가 정확해진다)
  grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $MGS_ENVS btrfs noatime,compress=zstd:1 0 0" >> /etc/fstab
  systemctl daemon-reload
  mount "$MGS_ENVS"
fi
fstype=$(findmnt -no FSTYPE --target "$MGS_ENVS")
[[ "$fstype" == "btrfs" ]] || die "$MGS_ENVS 가 btrfs 가 아닙니다($fstype). 빈 파티션을 --format 으로 지정하세요."
mkdir -p "$MGS_ENVS"/{templates,live,snap,run,state}
chmod 755 "$MGS_ENVS" "$MGS_ENVS"/{templates,live,snap,run,state}

say "공용 모델 저장소 ${MGS_MODELS}"
for d in checkpoints diffusion_models unet loras vae text_encoders clip clip_vision controlnet \
         upscale_models embeddings hypernetworks style_models gligen photomaker audio_encoders \
         model_patches latent_upscale_models vae_approx ollama inbox .incoming .trash; do
  mkdir -p "$MGS_MODELS/$d"
done
chown root:"$MGS_GROUP" "$MGS_MODELS"
chmod 2755 "$MGS_MODELS"
# 모델 업로드 폴더만 사용자 쓰기 허용 (허브가 검사한 뒤 공용 저장소로 옮긴다)
chown root:"$MGS_GROUP" "$MGS_MODELS/inbox"
chmod 2775 "$MGS_MODELS/inbox"

say "결과 저장소 ${MGS_RESULTS} (누구나 읽기·쓰기)"
mkdir -p "$MGS_RESULTS/.recycle"
chown root:"$MGS_GROUP" "$MGS_RESULTS" "$MGS_RESULTS/.recycle"
chmod 2775 "$MGS_RESULTS" "$MGS_RESULTS/.recycle"
# 새로 생기는 파일·폴더도 그룹 읽기·쓰기를 물려받게 한다
setfacl -R -m g:"$MGS_GROUP":rwX "$MGS_RESULTS"
setfacl -R -d -m g:"$MGS_GROUP":rwX "$MGS_RESULTS"

say "DB·채팅 데이터"
mkdir -p "$MGS_PG" /srv/open-webui

say "공용 런타임 파일 ${MGS_RUNTIME}"
mkdir -p "$MGS_RUNTIME"
cp -r "$REPO_DIR/runtime/." "$MGS_RUNTIME/"
chown -R root:root "$MGS_RUNTIME"
chmod -R a+rX "$MGS_RUNTIME"

say "완료. 다음: sudo bash install/03-samba.sh"
