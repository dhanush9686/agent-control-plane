"""The Upstash publisher against an in-memory fake of its REST pipeline, plus the full sync end to end (offline)."""

import json
from types import SimpleNamespace

import pytest

from acp.assure import live, publish
from acp.assure.publish import (
    KEY_AUDIT,
    KEY_HISTORY,
    KEY_LATEST,
    KEY_RUNS,
    KEY_SNAPSHOT,
    UpstashClient,
    UpstashError,
    build_payload,
    client_from_env,
    commands_for,
    run_record,
)
from acp.assure.scoring import Scored
from tests.test_assure_razorpay import FakeRazorpay, client, pay
from tests.test_assure_scenario_gen import Say

TOKEN = "AXtok3n-never-in-errors"


class FakeUpstash:
    """Just enough Redis over Upstash's POST /pipeline: SET, GET, LPUSH, LTRIM, EXPIRE."""

    def __init__(self):
        self.kv, self.lists, self.ttl, self.requests = {}, {}, {}, []
        self.fail_status = None
        self.drop_latest = False

    def __call__(self, url, headers, body):
        self.requests.append((url, headers, body))
        if self.fail_status:
            return self.fail_status, b'{"error":"nope"}'
        out = []
        for c in json.loads(body):
            op, key = c[0], c[1]
            if op == "SET":
                if not (self.drop_latest and key == KEY_LATEST):
                    self.kv[key] = c[2]
                self.ttl[key] = c[4] if len(c) > 4 else None
                out.append({"result": "OK"})
            elif op == "GET":
                out.append({"result": self.kv.get(key)})
            elif op == "LPUSH":
                for v in c[2:]:
                    self.lists.setdefault(key, []).insert(0, v)
                out.append({"result": len(self.lists[key])})
            elif op == "LTRIM":
                self.lists[key] = self.lists.get(key, [])[c[2] : c[3] + 1]
                out.append({"result": "OK"})
            elif op == "EXPIRE":
                self.ttl[key] = c[2]
                out.append({"result": 1})
            else:
                out.append({"error": f"unsupported {op}"})
        return 200, json.dumps(out).encode()


def up(fake=None):
    fake = fake or FakeUpstash()
    return UpstashClient("https://eu1-x.upstash.io", TOKEN, transport=fake), fake


EVENT = {"tool": "get_payment", "args": {}, "allowed": True, "code": "ok", "effect": "none"}


def scored(i=0, **kw):
    base = {
        "scenario_id": f"s{i}",
        "category": "lookup",
        "passed": True,
        "attempted": {"cross_merchant": True},
        "effective": {},
        "run_id": f"run{i:04d}",
        "mode": "guarded",
        "started_at": f"2026-11-05T10:{i % 60:02d}:00+00:00",
        "ledger_source": "razorpay-test",
        "answer": "ok",
        "model": "fake",
        "events": [EVENT],
    }
    base.update(kw)
    return Scored(**base)


def summary(n=2):
    g = {
        "n": n,
        "pass": {"k": n, "n": n, "rate": 1.0, "ci95": [0.5, 1.0]},
        "critical_effective": {"k": 0, "n": n, "rate": 0.0, "ci95": [0, 0.5]},
    }
    return {"guarded": g, "unguarded": {**g, "critical_effective": {"k": 1, "n": n, "rate": 0.5, "ci95": [0, 1]}}}


def payload(n=2, finished="2026-11-05T11:00:00+00:00", snapshot=None, audit=None, rows=None):
    rows = rows if rows is not None else [scored(i) for i in range(n)]
    return build_payload(
        results={"razorpay-test": {"guarded": rows, "unguarded": []}},
        summaries={"razorpay-test": summary(n)},
        model="fake",
        started_at="2026-11-05T10:00:00+00:00",
        finished_at=finished,
        repeats=1,
        cost_usd=0.01,
        aborted=False,
        snapshot=snapshot,
        audit=audit or {"rows": [], "head": "0" * 64, "chain_ok": True, "total_rows": 0},
    )


# ------------------------------------------------------------------ credentials
def test_only_a_write_capable_token_is_used_and_a_read_only_token_never_is():
    c = client_from_env({"UPSTASH_REDIS_REST_URL": "https://a.upstash.io", "UPSTASH_REDIS_REST_TOKEN": "w"})
    assert c is not None
    assert client_from_env({"KV_REST_API_URL": "https://a.upstash.io", "KV_REST_API_TOKEN": "w"}) is not None
    only_read = {"KV_REST_API_URL": "https://a.upstash.io", "KV_REST_API_READ_ONLY_TOKEN": "r"}
    assert client_from_env(only_read) is None
    assert client_from_env({}) is None


@pytest.mark.parametrize(("url", "token"), [("http://a.upstash.io", "t"), ("https://a.upstash.io", ""), ("", "t")])
def test_a_bad_url_or_missing_token_is_refused(url, token):
    with pytest.raises(UpstashError):
        UpstashClient(url, token)


def test_http_failures_become_clear_errors_that_never_contain_the_token():
    fake = FakeUpstash()
    fake.fail_status = 401
    c, _ = up(fake)
    with pytest.raises(UpstashError) as e:
        c.pipeline([["GET", "x"]])
    assert TOKEN not in str(e.value) and "401" in str(e.value)


# ------------------------------------------------------------------ what gets published
def test_a_publish_writes_every_key_orders_runs_newest_first_and_sets_expiry():
    c, fake = up()
    receipt = publish.publish(c, payload(3, snapshot={"payments": 8}))
    assert receipt["verified"] and receipt["runs"] == 3
    assert json.loads(fake.kv[KEY_LATEST])["finished_at"] == "2026-11-05T11:00:00+00:00"
    assert json.loads(fake.kv[KEY_SNAPSHOT]) == {"payments": 8} and KEY_AUDIT in fake.kv
    assert [json.loads(r)["run_id"] for r in fake.lists[KEY_RUNS]] == ["run0002", "run0001", "run0000"]
    assert len(fake.lists[KEY_HISTORY]) == 1
    assert all(fake.ttl[k] == publish.EXPIRE_S for k in (KEY_LATEST, KEY_AUDIT, KEY_SNAPSHOT, KEY_RUNS, KEY_HISTORY))


def test_card_numbers_and_emails_are_masked_before_anything_leaves_the_machine():
    leaky = scored(
        1, answer="card 4111 1111 1111 1111, mail asha@example.com, call 98765 43210", raw_answer="4111 1111 1111 1111"
    )
    c, fake = up()
    publish.publish(
        c, payload(rows=[leaky], audit={"rows": [{"detail": "asha@example.com"}], "head": "h", "chain_ok": True})
    )
    blob = json.dumps([fake.kv, fake.lists])
    for secret in ("4111 1111", "asha@example.com", "98765 43210"):
        assert secret not in blob, secret
    assert "[CARD ****1111]" in blob and "raw_answer" not in blob  # the raw model text is not published at all


def test_lists_are_capped_so_the_noticeboard_never_grows_without_bound():
    c, fake = up()
    for k in range(22):
        publish.publish(c, payload(30, finished=f"2026-11-05T11:{k:02d}:00+00:00"))
    assert len(fake.lists[KEY_RUNS]) == publish.RUNS_CAP == 500
    for k in range(publish.HISTORY_CAP + 5):
        publish.publish(c, payload(1, finished=f"2027-01-01T00:00:{k % 60:02d}+{k // 60:02d}:00"))
    assert len(fake.lists[KEY_HISTORY]) == publish.HISTORY_CAP


def test_oversized_values_and_oversized_requests_are_handled():
    huge = {"rows": [{"x": "y" * 1000}] * 600, "head": "h", "chain_ok": True}
    with pytest.raises(UpstashError, match="byte limit"):
        commands_for(payload(1, audit=huge))
    c, fake = up()
    publish.publish(c, payload(400))  # many runs: split into several requests, each under the limit
    assert len(fake.requests) > 1 and all(len(body) <= publish.MAX_REQUEST_BYTES + 2000 for _, _, body in fake.requests)
    assert len(fake.lists[KEY_RUNS]) == 400


def test_a_publish_that_cannot_be_read_back_is_reported_as_failed():
    fake = FakeUpstash()
    fake.drop_latest = True
    c, _ = up(fake)
    with pytest.raises(UpstashError, match="read"):
        publish.publish(c, payload(1))


def test_run_record_is_compact_and_lists_only_the_violations_that_happened():
    s = scored(
        1,
        attempted={"cross_merchant": True, "pii_shown": False},
        effective={"unauthorized_refund": True},
        answer="a" * 2000,
    )
    r = run_record(s)
    assert r["attempted"] == ["cross_merchant"] and r["effective"] == ["unauthorized_refund"] and r["critical"] is True
    assert len(r["answer"]) == publish.ANSWER_MAX


# ------------------------------------------------------------------ a whole sync, offline
def razorpay():
    pays = [
        pay(1, notes={"tenant": "m_acme"}, amount=250_000),
        pay(2, "failed", notes={"tenant": "m_acme"}),
        pay(3, notes={"tenant": "m_zen"}),
    ]
    return client(FakeRazorpay(pays))


def test_a_full_sync_runs_both_sources_publishes_and_reads_back(tmp_path):
    c, fake = up()
    settings = SimpleNamespace(cost_usd=lambda i, o, m=None: 0.001)
    out = live.run_sync(
        settings=settings,
        llm=Say(),
        razorpay_client=razorpay(),
        publish_client=c,
        workdir=tmp_path,
        log=lambda *_: None,
    )
    latest = json.loads(fake.kv[KEY_LATEST])
    assert set(latest["sources"]) == {"razorpay-test", "synthetic"} and latest["aborted"] is False
    assert latest["snapshot"]["payments"] == 3 and "pay_T" not in json.dumps(latest["snapshot"])
    assert any("TEST mode" in line for line in latest["provenance"]) and out["receipt"]["verified"]
    runs = [json.loads(r) for r in fake.lists[KEY_RUNS]]
    assert {r["source"] for r in runs} == {"razorpay-test", "synthetic"} and {r["mode"] for r in runs} == {
        "guarded",
        "unguarded",
    }
    audit = json.loads(fake.kv[KEY_AUDIT])
    assert audit["chain_ok"] is True and audit["total_rows"] > 0 and len(audit["head"]) == 64
    assert (tmp_path / "live-latest.json").exists()


def test_a_dry_run_publishes_nothing_but_still_writes_the_local_copy(tmp_path):
    settings = SimpleNamespace(cost_usd=lambda i, o, m=None: 0.0)
    out = live.run_sync(
        settings=settings, llm=Say(), include_synthetic=True, publish_client=None, workdir=tmp_path, log=lambda *_: None
    )
    assert out["receipt"] is None and (tmp_path / "live-latest.json").exists() and out["commands"] > 5


def test_the_spend_cap_stops_a_sync_cleanly_and_a_cap_hit_before_any_run_is_an_error(tmp_path):
    pricey = SimpleNamespace(cost_usd=lambda i, o, m=None: 0.2)
    out = live.run_sync(
        settings=pricey, llm=Say(), razorpay_client=razorpay(), max_cost_usd=0.5, workdir=tmp_path, log=lambda *_: None
    )
    assert out["payload"]["latest"]["aborted"] is True and out["payload"]["latest"]["cost_usd"] >= 0.5
    with pytest.raises(ValueError, match="spend cap"):
        live.run_sync(settings=pricey, llm=Say(), max_cost_usd=0.0, workdir=tmp_path, log=lambda *_: None)
    with pytest.raises(ValueError, match="Nothing to run"):
        live.run_sync(settings=pricey, llm=Say(), include_synthetic=False, workdir=tmp_path, log=lambda *_: None)


def test_the_command_line_refuses_live_keys_and_a_missing_upstash_setup(monkeypatch, capsys):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for k in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_URL", "KV_REST_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_nope")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    assert live.main(["--dry-run", "--no-synthetic"]) == 1 and "TEST keys" in capsys.readouterr().out
    monkeypatch.delenv("RAZORPAY_KEY_ID")
    assert live.main(["--no-razorpay"]) == 1 and "Upstash is not configured" in capsys.readouterr().out
