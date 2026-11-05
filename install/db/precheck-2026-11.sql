-- 2026-11 DB 보완 배포 전 사전 점검. 모든 결과가 0행이어야 제약이 전부 걸린다.
-- 행이 나와도 허브는 시작하지만, 해당 제약이 빠지거나 '검사 전' 으로 남고 허브 로그에 경고가 나온다.
--
--   cd /opt/mgs-src && sudo docker compose exec -T postgres psql -U mgs mgs -P pager=off < install/db/precheck-2026-11.sql

-- job_models_pkey: 한 작업 안의 같은 모델 이름
SELECT job_id, name, count(*) FROM job_models GROUP BY 1, 2 HAVING count(*) > 1;
-- job_models_pkey: 이름이 비어 있는 행 (PK 칸은 NULL 이면 안 된다)
SELECT job_id FROM job_models WHERE job_id IS NULL OR name IS NULL;
-- jobs_env_version_fk: 작업의 환경과 버전의 환경이 다름
SELECT j.id FROM jobs j JOIN env_versions v ON v.id = j.env_version_id WHERE j.env_id IS DISTINCT FROM v.env_id;
-- jobs_env_version_needs_env: 버전은 있는데 환경이 없음
SELECT id FROM jobs WHERE env_version_id IS NOT NULL AND env_id IS NULL;
-- jobs_state_check / jobs_outcome_check / incidents_kind_check: 허용값 밖
SELECT state, count(*) FROM jobs WHERE state NOT IN ('queued','starting','running','cleaning','finished') GROUP BY 1;
SELECT outcome, count(*) FROM jobs WHERE outcome NOT IN ('success','error','cancelled','timeout','rejected','lost') GROUP BY 1;
SELECT kind, count(*) FROM incidents WHERE kind NOT IN ('foreign_gpu_process','vram_not_released','auto_unblocked') GROUP BY 1;
