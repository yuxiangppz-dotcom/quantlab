import json
import os
import socket

import pytest

from quantlab import local_ui


def _free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def test_real_local_server_start_reuse_and_graceful_stop(tmp_path):
    port = _free_port()
    try:
        started = local_ui.manage_ui("start", port, runtime_root=tmp_path)
        assert started["status"] == "running" and not started["reused"]
        assert local_ui.manage_ui("start", port, runtime_root=tmp_path)["reused"]
        assert local_ui.manage_ui("status", port, runtime_root=tmp_path)["status"] == "running"
    finally:
        stopped = local_ui.manage_ui("stop", port, runtime_root=tmp_path)
    assert stopped["stopped_owned_process"]
    assert not local_ui._occupied(port)


def test_stop_refuses_a_reused_or_unrelated_pid(tmp_path):
    port = _free_port()
    state = {"pid": os.getpid(), "port": port, "start_ticks": local_ui._start_ticks(os.getpid())}
    (tmp_path / f"{port}.json").write_text(json.dumps(state))
    result = local_ui.manage_ui("stop", port, runtime_root=tmp_path)
    assert not result["stopped_owned_process"]
    assert os.getpid() == state["pid"]


def test_unowned_occupied_port_is_not_reused_or_stopped(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="unverified process"):
            local_ui.manage_ui("start", port, runtime_root=tmp_path)
        assert listener.fileno() != -1
