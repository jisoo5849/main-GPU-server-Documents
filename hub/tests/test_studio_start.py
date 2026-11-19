"""스튜디오 열기(studio/__init__.py) 시험: 시작 때 못 열면 허브를 다시 시작하지 않아도 다시 검사해서 연다.
11-18·19 메인에서 시작 때 모델 저장소를 못 읽어 레시피가 전부 꺼진 채, 허브를 손으로 다시 시작할 때까지 503 이었다.
DB·GPU·Ollama 없이 가짜로 돈다.

  cd hub && python tests/test_studio_start.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import types  # noqa: E402

import app  # noqa: E402,F401

# app.db 는 asyncpg 가 있어야 불러와지므로 가짜로 둔다 (test_studio_director 와 같은 방식)
_db = types.ModuleType("app.db")
sys.modules["app.db"] = _db
app.db = _db

# api.py 는 웹 프레임워크(starlette)를 불러온다. 이 시험은 api.state 만 쓰므로 가짜 모듈로 바꿔 끼운다
api = types.ModuleType("app.studio.api")


class _State:
    director = None
    ready = False
    reason = "스튜디오를 준비 중입니다."


api.state = _State()
sys.modules["app.studio.api"] = api

import app.studio as S  # noqa: E402
from app import models_index, promptops  # noqa: E402
from app.studio import recipes as R, store  # noqa: E402

S.api = api

RECS = R.load_all()
# 모델 파일을 쓰는 레시피 (목소리 레시피는 모델을 파일 이름이 아닌 크기 '1.7B' 로 고르므로 V10 대상이 아니다, 11-19)
WITH_MODELS = {rid for rid, r in RECS.items() if promptops.find_models(r["prompt"])}


class FakeDirector:
    def __init__(self):
        self.recipe_map = dict(RECS)
        self.disabled: set = set()
        self.recovered = 0

    def recipe_for(self, action, quality="fast", sound=False):
        if sound:
            return R.pick(self.recipe_map, self.disabled, action, "fast", "ltx")
        return R.pick(self.recipe_map, self.disabled, action, quality)

    async def recover(self):
        self.recovered += 1


class World:
    """가짜 바깥: 모델 저장소가 보이는지, Ollama 모델, 작업공간, DB 기록"""
    def __init__(self):
        self.models_seen = False
        self.ollama = None            # None 이면 Ollama 를 못 물어본 것(검사 건너뜀)
        self.env = {"slug": "studio"}
        self.saved: list = []
        self.index_calls: list = []
        self.cleanups = 0

    def patch(self):
        async def studio_env(slug):
            return self.env

        async def save_recipe_check(recipe, sha, ok, report):
            self.saved.append((recipe["id"], ok))

        async def ollama():
            return self.ollama

        async def cleanup():
            self.cleanups += 1

        def index(max_age=60.0):
            self.index_calls.append(max_age)
            return {"x.safetensors": [("checkpoints", "/m/x.safetensors")]} if self.models_seen else {}

        store.studio_env = studio_env
        store.save_recipe_check = save_recipe_check
        S._ollama_models = ollama
        S._cleanup_loop = cleanup
        models_index.index = index
        models_index.resolve = lambda name: ("checkpoints", "/m/" + name) if self.models_seen else None
        S._last_ok.clear()
        S._cleanup_started = False
        api.state.ready = False
        api.state.reason = "스튜디오를 준비 중입니다."


async def t_empty_model_store_then_opens_on_a_later_check():
    w = World(); w.patch()
    d = FakeDirector()
    assert not await S.try_open(d, 0)
    assert not api.state.ready and "모델 저장소가 비어" in api.state.reason and "자동으로 다시 확인" in api.state.reason
    assert d.disabled == WITH_MODELS                                 # 모델이 안 보이면 모델을 쓰는 레시피가 전부 꺼진다 (V10)
    assert len(w.saved) == len(RECS) and not any(ok for rid, ok in w.saved if rid in WITH_MODELS)
    # 같은 결과로 다시 검사하면 DB 에 다시 쌓지 않는다
    assert not await S.try_open(d, 1)
    assert len(w.saved) == len(RECS)
    assert 0 in w.index_calls                                         # 다시 검사할 때는 저장소를 새로 읽는다
    # 저장소가 보이게 되면 다음 검사에서 연다 (허브 재시작 없이)
    w.models_seen = True
    assert await S.try_open(d, 2)
    await asyncio.sleep(0)                                            # 정리 작업은 뒤에서 시작된다
    assert api.state.ready and d.recovered == 1 and w.cleanups == 1 and not d.disabled
    assert await S.try_open(d, 3) and w.cleanups == 1                 # 정리 작업은 한 번만 띄운다
    n = len(WITH_MODELS)
    assert len(w.saved) == len(RECS) + n and all(ok for _, ok in w.saved[len(RECS):])   # 바뀐 결과만 새로 남긴다


async def t_missing_ollama_model_keeps_it_closed_with_a_reason():
    w = World(); w.patch()
    w.models_seen = True
    w.ollama = {"something-else"}
    d = FakeDirector()
    assert not await S.try_open(d, 0)
    assert not api.state.ready and "AI 모델이 아직 없습니다" in api.state.reason and d.recovered == 0
    w.ollama = {S.settings.expand_model, S.settings.video_frame_model}
    assert await S.try_open(d, 1) and api.state.ready


async def t_missing_workspace_and_errors_do_not_open():
    w = World(); w.patch()
    w.models_seen = True
    w.env = None
    d = FakeDirector()
    assert not await S.try_open(d, 0) and "채팅 작업공간" in api.state.reason

    w.env = {"slug": "studio"}

    async def boom():
        raise RuntimeError("recover failed")
    d.recover = boom
    assert not await S.try_open(d, 0) and not api.state.ready         # 예외면 열지 않고 다시 검사에 맡긴다


async def t_retry_loop_opens_when_the_store_comes_back():
    w = World(); w.patch()
    d = FakeDirector()
    S.RETRY_FIRST, S.RETRY_MAX = 0.01, 0.02
    assert not await S.try_open(d, 0)
    task = asyncio.ensure_future(S._retry_open(d))
    await asyncio.sleep(0.05)
    assert not api.state.ready and not task.done()                   # 아직 비어 있으면 계속 기다린다
    w.models_seen = True
    await asyncio.wait_for(task, 2)
    assert api.state.ready and d.recovered == 1


async def t_failure_message_names_the_store_state():
    w = World(); w.patch()
    d = FakeDirector()
    seen, files = S._store_state()
    assert files == 0
    w.models_seen = True
    assert S._store_state()[1] == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for t in tests:
        asyncio.run(t())
        print("ok", t.__name__)
    print("ok studio_start")
