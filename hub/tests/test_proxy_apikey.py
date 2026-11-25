"""환경 포트(8101~)의 API 키 접속 (11-25): 브라우저는 쿠키, MCP·스크립트는 Authorization: Bearer mgs_… 로 들어온다.
DB·네트워크 없이 가짜로: proxy._authorize 의 판단과, 키로 들어온 요청이 새 키를 못 만드는지(panel.handle)."""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# 개발 PC 에 없는 외부 모듈(httpx·starlette 등)은 가짜로 끼운다: proxy 를 불러오려고만. 이 시험은 네트워크·DB 를 쓰지 않는다
class _Dummy:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Dummy()

    def __getattr__(self, name):
        return _Dummy()


class _StubModule(types.ModuleType):
    __path__ = []                        # 하위 모듈(starlette.requests 등)도 가져올 수 있게

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return type(name, (Exception,) if name.endswith(("Error", "Exception")) else (_Dummy,), {})


def _import_proxy():
    for _ in range(40):
        try:
            from app import panel, proxy
            return proxy, panel
        except ModuleNotFoundError as e:
            name = e.name or ""
            if not name or name.split(".")[0] == "app":
                raise
            sys.modules[name] = _StubModule(name)
            for k in [k for k in sys.modules if k.startswith("app.") and k != "app"]:
                del sys.modules[k]       # 불러오다 만 모듈을 지우고 다시
    raise ImportError("가짜 모듈로도 proxy 를 불러오지 못했다")


P, PANEL = _import_proxy()
SEC = P.security

OWNER = {"id": 1, "role": "user", "username": "alice"}
OTHER = {"id": 2, "role": "user", "username": "bob"}
ADMIN = {"id": 9, "role": "admin", "username": "admin"}
ENV = {"id": 5, "slug": "alice-main", "owner_id": 1}


class Conn:
    """_authorize 가 보는 것만: scope(포트)·cookies·headers·state"""

    def __init__(self, cookie=None, bearer=None):
        self.scope = {"server": ("main", 8101)}
        self.cookies = {SEC.SESSION_COOKIE: cookie} if cookie else {}
        self.headers = {"authorization": f"Bearer {bearer}"} if bearer else {}
        self.state = types.SimpleNamespace()


def run(conn, sessions=None, keys=None, env=ENV):
    """sessions: 쿠키 값 → 사용자, keys: API 키 → 사용자. 조회한 키 목록도 함께 돌려준다."""
    sessions, keys, looked = sessions or {}, keys or {}, []

    async def env_by_port(_port):
        return env

    async def session_user(sid):
        return sessions.get(sid)

    async def api_key_user(key):
        looked.append(key)
        return keys.get(key)

    P.envs.env_by_port, SEC.session_user, SEC.api_key_user = env_by_port, session_user, api_key_user
    return asyncio.run(P._authorize(conn)), looked


def t_cookie_login_still_works_and_never_looks_up_keys():
    (env, user, problem), looked = run(Conn(cookie="c1"), sessions={"c1": OWNER}, keys={"mgs_k": ADMIN})
    assert (env, user, problem) == (ENV, OWNER, None)
    assert looked == []                                       # 쿠키가 유효하면 키는 보지도 않는다
    c = Conn(cookie="c1", bearer="mgs_k")
    (_, user, _), _ = run(c, sessions={"c1": OWNER}, keys={"mgs_k": ADMIN})
    assert user is OWNER and not getattr(c.state, "via_api_key", False)   # 둘 다 오면 쿠키가 먼저


def t_owner_api_key_is_accepted_and_marked():
    c = Conn(bearer="mgs_k")
    (env, user, problem), looked = run(c, keys={"mgs_k": OWNER})
    assert (env, user, problem) == (ENV, OWNER, None)
    assert looked == ["mgs_k"] and c.state.via_api_key is True


def t_expired_cookie_falls_back_to_a_valid_key():
    c = Conn(cookie="old", bearer="mgs_k")
    (_, user, problem), _ = run(c, sessions={}, keys={"mgs_k": OWNER})
    assert user is OWNER and problem is None and c.state.via_api_key is True


def t_no_credentials_or_unknown_key_means_login():
    c = Conn()
    (_, user, problem), looked = run(c)
    assert user is None and problem == "login" and looked == [None]    # 키가 없으면 DB 조회 없이(api_key_user(None)) 끝
    assert not getattr(c.state, "via_api_key", False)
    c = Conn(bearer="mgs_wrong")
    (_, user, problem), _ = run(c, keys={"mgs_k": OWNER})
    assert user is None and problem == "login" and not getattr(c.state, "via_api_key", False)


def t_key_of_another_user_cannot_open_this_workspace_but_admin_key_can():
    (_, user, problem), _ = run(Conn(bearer="mgs_b"), keys={"mgs_b": OTHER})
    assert user is OTHER and problem == "forbidden"
    (_, user, problem), _ = run(Conn(bearer="mgs_a"), keys={"mgs_a": ADMIN})
    assert user is ADMIN and problem is None                  # 관리자는 쿠키로도 남의 작업공간을 연다. 키도 같다


def t_unknown_port_stays_env_problem():
    (env, user, problem), looked = run(Conn(bearer="mgs_k"), keys={"mgs_k": OWNER}, env=None)
    assert (env, user, problem) == (None, None, "env") and looked == []


class FakeResp:
    def __init__(self, body=None, status_code=200, **_):
        self.body, self.status_code = body, status_code


class FakeRequest:
    method = "POST"
    client = None

    def __init__(self, via_api_key):
        self.state = types.SimpleNamespace()
        if via_api_key:
            self.state.via_api_key = True

    async def json(self):
        return {"label": "test"}


def issue_key(via_api_key):
    """panel.handle 의 POST /mgs/api/keys. 발급 함수가 불렸는지도 돌려준다."""
    made, logs = [], []

    async def create_api_key(uid, label):
        made.append((uid, label))
        return "mgs_new"

    async def log_access(*a, **k):
        logs.append(a)

    PANEL.JSONResponse = FakeResp
    PANEL.security.create_api_key, PANEL.db.log_access = create_api_key, log_access
    resp = asyncio.run(PANEL.handle(FakeRequest(via_api_key), ENV, OWNER, "/mgs/api/keys"))
    return resp, made


def t_browser_login_can_issue_a_key_to_the_owner():
    resp, made = issue_key(via_api_key=False)
    assert resp.status_code == 200 and resp.body == {"key": "mgs_new"} and made == [(1, "test")]


def t_a_key_cannot_mint_another_key():
    resp, made = issue_key(via_api_key=True)
    assert resp.status_code == 403 and made == []


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(f"{len(tests)}개 통과")
