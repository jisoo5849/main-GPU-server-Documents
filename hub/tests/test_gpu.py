"""gpu.parse_starttime·proc_key 단위 테스트. NVML 없이 실행된다.

  cd hub && python tests/test_gpu.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import gpu  # noqa: E402


def test_plain_name():
    stat = "1234 (python3) S 1 1234 1234 0 -1 4194560 100 0 0 0 5 3 0 0 20 0 8 0 987654 1000 200"
    assert gpu.parse_starttime(stat) == "987654"


def test_name_with_space_and_paren():
    stat = "42 (Web Content (x)) S 1 42 42 0 -1 4194560 100 0 0 0 5 3 0 0 20 0 8 0 555 1000 200"
    assert gpu.parse_starttime(stat) == "555"


def test_unreadable_pid_is_none():
    # 시작 시각을 못 읽으면 다른 프로세스와 한 줄로 합쳐지지 않도록 None 이어야 한다
    assert gpu.proc_key(2 ** 31 - 1) is None


if __name__ == "__main__":
    test_plain_name()
    test_name_with_space_and_paren()
    test_unreadable_pid_is_none()
    print("test_gpu ok")
