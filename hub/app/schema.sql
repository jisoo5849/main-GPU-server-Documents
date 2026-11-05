-- 메인 GPU 서버 DB. 허브가 시작할 때마다 실행되며, 이미 있는 것은 건너뛴다.

CREATE TABLE IF NOT EXISTS users (
    id           serial PRIMARY KEY,
    username     text UNIQUE NOT NULL,
    display_name text,
    pw_hash      text NOT NULL,
    role         text NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    linux_uid    int  NOT NULL,
    active       bool NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id         text PRIMARY KEY,
    user_id    int NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    ended_at   timestamptz,
    end_reason text,
    ip         inet,
    user_agent text
);

-- 접속 기록: login_ok, login_fail, logout, session_expired, portal_select, env_open, env_create,
--           env_rollback, api_key_create, api_key_use, admin_action, admin_cli, model_intake,
--           prompt_expand, image_inspect
CREATE TABLE IF NOT EXISTS access_log (
    id      bigserial PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    user_id int REFERENCES users(id),
    ip      inet,
    event   text NOT NULL,
    detail  jsonb
);
CREATE INDEX IF NOT EXISTS access_log_ts ON access_log(ts DESC);

CREATE TABLE IF NOT EXISTS api_keys (
    id           serial PRIMARY KEY,
    user_id      int NOT NULL REFERENCES users(id),
    prefix       text NOT NULL,
    key_hash     text UNIQUE NOT NULL,
    label        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);

-- 작업공간(환경). 사용자 기본 환경 'main' 과 프로젝트 환경
CREATE TABLE IF NOT EXISTS envs (
    id         serial PRIMARY KEY,
    owner_id   int  NOT NULL REFERENCES users(id),
    name       text NOT NULL,
    slug       text UNIQUE NOT NULL,
    port       int  UNIQUE NOT NULL,
    state      text NOT NULL DEFAULT 'running',
    template   text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name)
);

-- 환경 버전: 실행 접수 때 찍은 읽기 전용 스냅샷
CREATE TABLE IF NOT EXISTS env_versions (
    id            serial PRIMARY KEY,
    env_id        int NOT NULL REFERENCES envs(id),
    vno           int NOT NULL,
    snapshot_path text NOT NULL,
    btrfs_gen     bigint,
    comfy_commit  text,
    nodes         jsonb,
    pip_freeze    text,
    keep          bool NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz,
    UNIQUE (env_id, vno)
);

CREATE SEQUENCE IF NOT EXISTS jobs_seq;

-- 작업 대기열이자 작업 기록. seq(접수 순번)만이 실행 순서를 정한다.
-- state: queued → starting → running → cleaning → finished
-- outcome: success | error | cancelled | timeout | rejected | lost
CREATE TABLE IF NOT EXISTS jobs (
    id              uuid PRIMARY KEY,
    seq             bigint UNIQUE NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('comfy', 'llm', 'embed')),
    user_id         int NOT NULL REFERENCES users(id),
    env_id          int REFERENCES envs(id),
    env_version_id  int REFERENCES env_versions(id),
    client_ip       inet,
    client_id       text,
    api_endpoint    text,
    model           text,
    state           text NOT NULL DEFAULT 'queued',
    outcome         text,
    prompt          jsonb,
    workflow        jsonb,
    extra_data      jsonb,
    partial_targets jsonb,
    request         jsonb,
    submitted_at    timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    exec_started_at timestamptz,
    exec_ended_at   timestamptz,
    finished_at     timestamptz,
    wait_ms         int,
    cold_start_ms   int,
    run_ms          int,
    cleanup_ms      int,
    cleanup_method  text,
    vram_peak_mb    int,
    vram_after_mb   int,
    error           jsonb,
    comfy_job       jsonb,
    comfy_history   jsonb
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(seq) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS jobs_env ON jobs(env_id, seq DESC);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, seq DESC);
-- GPU를 쓰는 작업은 DB 규칙으로도 한 건만 존재할 수 있다
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_gpu_holder ON jobs((true))
    WHERE state IN ('starting', 'running', 'cleaning');

CREATE TABLE IF NOT EXISTS job_models (
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind   text,
    name   text NOT NULL,
    path   text,
    size   bigint,
    mtime  double precision
);
CREATE INDEX IF NOT EXISTS job_models_name ON job_models(name);

CREATE TABLE IF NOT EXISTS job_outputs (
    id         bigserial PRIMARY KEY,
    job_id     uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    node_id    text,
    rel_path   text NOT NULL,
    media      text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS job_outputs_job ON job_outputs(job_id);

-- LLM·임베딩 호출 (대기열을 거친 호출과, 실행 중인 워크플로우 안에서 나온 내부 호출 모두)
CREATE TABLE IF NOT EXISTS llm_calls (
    id                bigserial PRIMARY KEY,
    ts                timestamptz NOT NULL DEFAULT now(),
    job_id            uuid REFERENCES jobs(id) ON DELETE SET NULL,
    parent_job_id     uuid REFERENCES jobs(id) ON DELETE SET NULL,
    user_id           int REFERENCES users(id),
    endpoint          text,
    model             text,
    nested            bool NOT NULL DEFAULT false,
    prompt_tokens     int,
    completion_tokens int,
    duration_ms       int,
    status            int
);

CREATE TABLE IF NOT EXISTS incidents (
    id     bigserial PRIMARY KEY,
    ts     timestamptz NOT NULL DEFAULT now(),
    kind   text NOT NULL,
    detail jsonb
);

-- paused: 관리자 일시 정지 / blocked: 메모리 해제 확인 실패로 대기열 정지
CREATE TABLE IF NOT EXISTS settings (
    key   text PRIMARY KEY,
    value jsonb
);

-- ── 2026-11 보완. 허브가 시작할 때마다 실행되므로 모두 멱등으로 쓴다 ──────────
-- 제약은 기존 데이터가 어긋나도 허브 시작을 막지 않게 넣는다.
--   CHECK·FK 는 NOT VALID 로 붙여 새 행부터 검사하고, 기존 행 검사(VALIDATE)는 실패하면 경고만 남긴다.
--   빠졌거나 검사 못 한 제약은 db.init() 이 허브 로그에 경고로 알린다.

-- B1 감시 기록: 같은 프로세스(proc_key)는 한 줄로 두고 마지막 감지 시각·횟수만 갱신
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS proc_key   text;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS last_seen  timestamptz;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS seen_count int NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS incidents_proc ON incidents(kind, proc_key) WHERE proc_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS incidents_seen ON incidents((coalesce(last_seen, ts)) DESC);

-- B2 job_models: 한 작업 안에서 모델 이름은 중복되지 않는다 (promptops.find_models 가 거른다)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'job_models_pkey') THEN
    BEGIN
      ALTER TABLE job_models ADD CONSTRAINT job_models_pkey PRIMARY KEY (job_id, name);
    EXCEPTION WHEN unique_violation OR not_null_violation THEN
      RAISE WARNING 'job_models_pkey 를 걸지 못함 (중복 행 있음): %', SQLERRM;
    END;
  END IF;
  -- PK 색인이 job_id 로 시작하므로 겹친다. PK 를 못 걸었으면 조회용으로 남긴다
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'job_models_pkey') THEN
    DROP INDEX IF EXISTS job_models_job;
  ELSE
    CREATE INDEX IF NOT EXISTS job_models_job ON job_models(job_id);
  END IF;
END $$;

-- B3 jobs 의 환경 버전은 반드시 같은 환경의 버전이어야 한다.
--    env_versions 는 지우지 않고 deleted_at 만 찍으므로(envs.gc_versions) 버전 정리를 막지 않는다.
--    FK 는 MATCH SIMPLE 이라 env_id 가 NULL 이면 검사를 건너뛴다. "버전만 있고 환경이 없는" 행은 CHECK 로 막는다.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'env_versions_env_id_id_key') THEN
    ALTER TABLE env_versions ADD CONSTRAINT env_versions_env_id_id_key UNIQUE (env_id, id);  -- id 가 PK 라 실패하지 않는다
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_env_version_fk') THEN
    ALTER TABLE jobs ADD CONSTRAINT jobs_env_version_fk
      FOREIGN KEY (env_id, env_version_id) REFERENCES env_versions(env_id, id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_env_version_needs_env') THEN
    ALTER TABLE jobs ADD CONSTRAINT jobs_env_version_needs_env
      CHECK (env_version_id IS NULL OR env_id IS NOT NULL) NOT VALID;
  END IF;
END $$;

-- B4 주석에만 있던 허용값을 DB 가 막는다.
--    새 state·outcome·incidents.kind 값을 코드에 추가할 때는 같은 커밋에서 이 CHECK 도 바꾼다.
--    빠뜨리면 그 값을 쓰는 UPDATE·INSERT 가 실패해 작업이 'cleaning' 에 남거나 감시 기록이 빠진다.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_state_check') THEN
    ALTER TABLE jobs ADD CONSTRAINT jobs_state_check
      CHECK (state IN ('queued', 'starting', 'running', 'cleaning', 'finished')) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'jobs_outcome_check') THEN
    ALTER TABLE jobs ADD CONSTRAINT jobs_outcome_check
      CHECK (outcome IS NULL OR outcome IN ('success', 'error', 'cancelled', 'timeout', 'rejected', 'lost')) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'incidents_kind_check') THEN
    ALTER TABLE incidents ADD CONSTRAINT incidents_kind_check
      CHECK (kind IN ('foreign_gpu_process', 'vram_not_released', 'auto_unblocked')) NOT VALID;
  END IF;
END $$;

-- B3·B4 기존 행 검사. 통과하면 다음 시작부터는 건너뛴다. 위반 행이 있으면 경고만 남기고 허브는 시작한다
DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conrelid::regclass AS tbl, conname FROM pg_constraint
           WHERE NOT convalidated AND conname IN ('jobs_env_version_fk', 'jobs_env_version_needs_env',
                 'jobs_state_check', 'jobs_outcome_check', 'incidents_kind_check') LOOP
    BEGIN
      EXECUTE format('ALTER TABLE %s VALIDATE CONSTRAINT %I', c.tbl, c.conname);
    EXCEPTION WHEN check_violation OR foreign_key_violation THEN
      RAISE WARNING '% 기존 행 검사 실패 (새 행은 검사 중): %', c.conname, SQLERRM;
    END;
  END LOOP;
END $$;

-- B5 사용자별 조회, FK 삭제 시 검사용 색인
CREATE INDEX IF NOT EXISTS access_log_user      ON access_log(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS sessions_user        ON sessions(user_id);
CREATE INDEX IF NOT EXISTS llm_calls_job        ON llm_calls(job_id);
CREATE INDEX IF NOT EXISTS llm_calls_parent_job ON llm_calls(parent_job_id);
