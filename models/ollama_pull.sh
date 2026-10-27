#!/usr/bin/env bash
# Ollama LLM·임베딩 모델 받기 (메인 GPU 서버의 Ollama 컨테이너, 저장 위치 /home/mgs/models/ollama)
#   sudo bash /home/elixir/mgs-install/models/ollama_pull.sh            # 추천 5개
#   sudo bash /home/elixir/mgs-install/models/ollama_pull.sh all        # 추천 + 선택 15개
set -uo pipefail
REC=(qwen3.5:9b gemma4:e4b qwen3.8:27b qwen3-vl:8b-instruct bge-m3)
OPT=(qwen3.5:4b gemma4:26b qwen3.6:35b gpt-oss:20b minicpm-v4.5:8b nomic-embed-text nomic-embed-text-v2-moe mxbai-embed-large embeddinggemma-2:270m qwen3-embedding:8b)
TAGS=("${REC[@]}"); [[ "${1:-}" == "all" ]] && TAGS+=("${OPT[@]}")
cd /opt/mgs-src || exit 1
fail=0
for t in "${TAGS[@]}"; do
  echo "==> ollama pull $t"
  docker compose exec -T ollama ollama pull "$t" || { echo "[x] 실패: $t"; fail=$((fail+1)); }
done
echo; docker compose exec -T ollama ollama list
echo "끝. 실패 $fail 개"
