"""The local web app must not be usable by other websites open in the same browser:
Host check (DNS rebinding), Origin check (cross-site requests), per-process token on every /api/ call,
JSON-only POSTs. Runs a real server on an ephemeral 127.0.0.1 port; no paid calls are made."""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from review_intel import server


@pytest.fixture(scope="module")
def srv():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()


def req(port, method, path, *, host=None, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}"}
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def tok():
    return {server.TOKEN_HEADER: server.TOKEN}


def test_binds_loopback_only():
    import inspect
    assert '("127.0.0.1", port)' in inspect.getsource(server.serve)


def test_index_served_with_token_on_good_host(srv):
    status, body = req(srv, "GET", "/")
    assert status == 200 and server.TOKEN.encode() in body
    status, body = req(srv, "GET", "/", host=f"localhost:{srv}")
    assert status == 200


@pytest.mark.parametrize("bad_host", ["evil.example.com", "evil.example.com:{p}", "127.0.0.1", "127.0.0.1:1",
                                      "0.0.0.0:{p}", "localhost.evil.com:{p}"])
def test_bad_host_rejected_everywhere(srv, bad_host):
    h = bad_host.format(p=srv)
    for path in ("/", "/api/health", "/template.csv"):
        status, body = req(srv, "GET", path, host=h, headers=tok())
        assert status == 403, (path, h)
        assert server.TOKEN.encode() not in body  # a rebinding page must never read the token


@pytest.mark.parametrize("origin", ["https://evil.example.com", "null", "http://localhost:1"])
def test_foreign_origin_rejected(srv, origin):
    status, _ = req(srv, "GET", "/api/health", headers={**tok(), "Origin": origin})
    assert status == 403
    status, _ = req(srv, "POST", "/api/jobs", headers={**tok(), "Origin": origin, "Content-Type": "application/json"},
                    body=b"{}")
    assert status == 403


def test_own_origin_allowed(srv):
    status, _ = req(srv, "GET", "/api/health", headers={**tok(), "Origin": f"http://127.0.0.1:{srv}"})
    assert status == 200


@pytest.mark.parametrize("path", ["/api/health", "/api/jobs/abc", "/api/jobs/abc/csv"])
@pytest.mark.parametrize("headers", [{}, {server.TOKEN_HEADER: "wrong"}, {server.TOKEN_HEADER: ""}])
def test_api_get_requires_token(srv, path, headers):
    status, _ = req(srv, "GET", path, headers=headers)
    assert status == 403


def test_api_post_requires_token(srv):
    body = json.dumps({"filename": "x.csv", "csv": "domain,city,state,zip\nx.com,A,TX,78701\n"}).encode()
    status, _ = req(srv, "POST", "/api/jobs", headers={"Content-Type": "application/json"}, body=body)
    assert status == 403


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"])
def test_post_requires_json_content_type(srv, ctype):
    headers = dict(tok())
    if ctype:
        headers["Content-Type"] = ctype
    status, _ = req(srv, "POST", "/api/jobs", headers=headers, body=b'{"csv": ""}')
    assert status == 415


def test_valid_request_reaches_handler(srv):
    status, body = req(srv, "GET", "/api/health", headers=tok())
    assert status == 200 and "treg_key" in json.loads(body)
    # a well-formed, authorised POST gets past the guards to input validation (no job is started)
    status, body = req(srv, "POST", "/api/jobs", headers={**tok(), "Content-Type": "application/json; charset=utf-8"},
                       body=json.dumps({"filename": "x.csv", "csv": "domain,city\nx.com,Austin\n"}).encode())
    assert status == 400 and "Missing column" in json.loads(body)["error"]
    status, _ = req(srv, "GET", "/api/jobs/nope/csv", headers=tok())
    assert status == 404
