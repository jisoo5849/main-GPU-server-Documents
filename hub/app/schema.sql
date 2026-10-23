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
--           env_rollback, api_key_use, admin_action, model_intake
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
CREATE INDEX IF NOT EXISTS job_models_job ON job_models(job_id);
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
