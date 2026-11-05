-- 기존 foreign_gpu_process 반복 기록을 프로세스별 한 줄로 합친다 (2026-11 DB 보완 배포 뒤 한 번).
-- 새 칸(last_seen, seen_count)이 필요하므로 보완 코드가 배포되어 허브가 한 번 시작한 뒤 실행한다.
--
--   cd /opt/mgs-src && sudo docker compose exec -T postgres psql -U mgs mgs -v ON_ERROR_STOP=1 -P pager=off \
--        < install/db/incidents-merge-2026-11.sql
--
-- 같은 프로세스 판단: 예전 기록에는 시작 시각이 없으므로 (pid, 이름, 컨테이너)가 같고
--   앞 기록과 30분 안에 이어지는 것만 한 묶음으로 본다. 감시는 10분마다 다시 기록했으므로
--   계속 떠 있던 프로세스는 한 묶음이 되고, 재부팅 뒤 PID 가 재사용된 경우나 허브가 멈췄던 구간은 따로 남는다.
-- 묶음마다 가장 이른 줄 하나를 남기고 last_seen(마지막 기록), seen_count(기록 수),
--   detail.max_mem_mb(최대 사용량)를 채운 뒤 나머지 줄을 지운다.
-- 여러 번 실행해도 결과가 같다 (이미 합친 줄은 last_seen 이 있어 다시 건드리지 않는다).
-- 되돌리기: incidents_backup_20261105 에 실행 전 전체가 남는다. 확인이 끝나면 DROP TABLE 로 지운다.

BEGIN;

CREATE TABLE IF NOT EXISTS incidents_backup_20261105 AS TABLE incidents;

SELECT count(*) AS before_rows FROM incidents WHERE kind = 'foreign_gpu_process';

CREATE TEMP TABLE merge_plan ON COMMIT DROP AS
WITH legacy AS (
    SELECT id, ts,
           detail->>'pid'                    AS pid,
           detail->>'name'                   AS name,
           coalesce(detail->>'container', '') AS container,
           coalesce((detail->>'mem_mb')::int, 0) AS mem
    FROM incidents
    WHERE kind = 'foreign_gpu_process' AND proc_key IS NULL AND last_seen IS NULL
), marked AS (
    SELECT *, CASE WHEN ts - lag(ts) OVER w > interval '30 minutes' THEN 1 ELSE 0 END AS brk
    FROM legacy
    WINDOW w AS (PARTITION BY pid, name, container ORDER BY ts, id)
), grouped AS (
    SELECT *, sum(brk) OVER (PARTITION BY pid, name, container ORDER BY ts, id) AS grp
    FROM marked
)
SELECT (array_agg(id ORDER BY ts, id))[1] AS keep_id,
       array_agg(id)                      AS ids,
       max(ts)                            AS last_seen,
       count(*)                           AS n,
       max(mem)                           AS max_mem
FROM grouped
GROUP BY pid, name, container, grp
HAVING count(*) > 1;

SELECT count(*) AS groups, coalesce(sum(n), 0) AS rows_in_groups, coalesce(sum(n - 1), 0) AS rows_to_delete
FROM merge_plan;

UPDATE incidents i
SET last_seen  = p.last_seen,
    seen_count = p.n,
    detail     = i.detail || jsonb_build_object('max_mem_mb', p.max_mem)
FROM merge_plan p
WHERE i.id = p.keep_id;

DELETE FROM incidents i
USING merge_plan p
WHERE i.id = ANY(p.ids) AND i.id <> p.keep_id;

SELECT count(*) AS after_rows FROM incidents WHERE kind = 'foreign_gpu_process';
SELECT detail->>'name' AS name, count(*) AS rows, sum(seen_count) AS detections,
       max((detail->>'max_mem_mb')::int) AS max_mem_mb
FROM incidents WHERE kind = 'foreign_gpu_process'
GROUP BY 1 ORDER BY detections DESC LIMIT 20;

COMMIT;
