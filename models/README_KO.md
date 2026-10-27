# 메인 GPU 서버 모델 일괄 다운로드 (2026-10-26)

ComfyUI v0.42.5 공식 템플릿 635개가 쓰는 모델 **348개 파일(약 2.85TB)**을 라이선스와 종류로 나눈 목록입니다.
받는 곳은 `/home/mgs/models` (작업자 화면에서는 `/srv/models`로 보임, 재시작 없이 목록에 나타남). 디스크 여유는 약 1TB입니다.

## 추천 묶음: `0_core_t2i_i2v` — 텍스트→이미지→영상 (37개, 약 381GB)

라이선스는 Apache-2.0·MIT와 LTX-2(그룹 매출 US$10M 미만 무료, 확인됨)뿐입니다. 비상업·매출 US$1M 조건·지역 제한 모델은 없습니다.

| 단계 | 템플릿 (이 묶음으로 바로 실행 가능) | 크기 |
|---|---|---|
| 텍스트→이미지 | Qwen-Image 2512(+2스텝 LoRA), FLUX.1 schnell, HiDream-O1(+dev), Z-Image(기본·Turbo int8), FLUX.2 Klein, Chroma1-Radiance, LongCat | 159 GB |
| 첫 화면 다듬기 | Qwen-Image 2512 ControlNet, Qwen-Image-Edit 2511, FLUX.2 Klein 편집, LongCat 편집 | 42 GB |
| 이미지→영상·텍스트→영상 | Wan2.2 14B T2V·Fun Camera·Fun Control, Wan VACE 첫·끝 화면, WanMove, Causal Forcing I2V, LTX-2.3 첫·끝 화면·IC-LoRA·ID-LoRA | 179 GB |

이미 있는 Wan2.2 I2V 14B(+4스텝 LoRA), LTX-2.3·2.5, Z-Image Turbo, Qwen-Image 2.1 등은 그대로 함께 쓰입니다.

```bash
cd docs/models
python3 -I download_models.py manifests/0_core_t2i_i2v.json logs/0_core /home/mgs/models
```
1GbE 기준 약 1시간. 받은 뒤 `/home` 여유 약 600GB(Ollama 추천 38GB를 더 받으면 약 560GB).

## 묶음별 크기 (전체, 이미 있는 파일은 빼고 계산)

| 묶음 (manifests/) | 라이선스 | 받을 파일 | 크기 |
|---|---|---|---|
| `1_free_video` | 상업 가능 (Apache·MIT 등) | 38 | 424 GB |
| `1_free_image` | 상업 가능 | 85 | 852 GB |
| `1_free_other` (오디오·3D·도구·인코더) | 상업 가능 | 59 | 308 GB |
| `2_conditional_video` / `_image` / `_other` | 매출 조건부 무료 (LTX: US$10M 미만 — 확인됨 / Krea 2·Stability: US$1M 미만) | 9 / 10 / 7 | 118 / 118 / 43 GB |
| `3_noncommercial_image` / `_other` | **비상업 전용** (FLUX.1/2 dev, Ideogram 4, Qwen-Image 2.1 등) | 27 / 9 | 312 / 30 GB |

**받지 않는 것 (목록에 넣지 않음)**
- 지역 제한: MiniMax H3, **Tencent Hunyuan** — 라이선스가 대한민국을 제외 (원문 확인)
- 로그인·약관 동의 필요(gated): FLUX 3 Klein, FLUX.2 Klein 일부, LTX-2.5 일부
- 라이선스 표기 없음: 14개 (확인 전까지 보류)

## 받는 방법 (sudo 불필요)

```bash
cd docs/models
python3 -I download_models.py manifests/1_free_video.json logs/1_free_video /home/mgs/models
```
- 묶음 이름만 바꿔 여러 번 실행합니다. 크기·sha256 확인, 끊기면 이어받기, 이미 받은 파일은 건너뜀.
- 1GbE 기준 약 **400GB에 1시간**.

## Ollama LLM·임베딩 (지난번 추천 목록)

```bash
sudo bash /opt/mgs-src/models/ollama_pull.sh        # 추천 5개 (38GB)
sudo bash /opt/mgs-src/models/ollama_pull.sh all    # 추천+선택 15개 (110GB)
```
저장 위치는 `/home/mgs/models/ollama`(이제 `/home`이라 공간 문제 없음).

## 파일

- `manifests/*.json` — 묶음별 목록 (주소, 크기, sha256, 라이선스, 쓰는 템플릿)
- `summary.json` — 위 표의 원자료
- `download_models.py` — 다운로드 스크립트, `ollama_pull.sh`, `ollama_models.json`
