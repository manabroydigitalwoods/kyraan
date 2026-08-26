from kyraan.model_router import router


def test_no_pricing_configured_means_free():
    tier_cfg = {}
    usage = router.Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert router._cost_usd(tier_cfg, usage) == 0.0


def test_cost_computed_per_million_tokens():
    tier_cfg = {"pricing": {"input_per_million": 0.05, "output_per_million": 0.40}}
    usage = router.Usage(input_tokens=1000, output_tokens=500)
    assert router._cost_usd(tier_cfg, usage) == (1000 / 1_000_000 * 0.05) + (500 / 1_000_000 * 0.40)


def test_missing_token_counts_treated_as_zero():
    tier_cfg = {"pricing": {"input_per_million": 1.0, "output_per_million": 1.0}}
    usage = router.Usage(input_tokens=None, output_tokens=None)
    assert router._cost_usd(tier_cfg, usage) == 0.0


def test_ledger_accumulates_per_day_and_survives_restart(monkeypatch, tmp_path):
    """session_cost_usd resets on restart; the ledger must not — it's what
    the daily budget cap is enforced against."""
    monkeypatch.setattr(router, "COST_LEDGER_PATH", tmp_path / "cost_ledger.json")
    router._record_cost(0.10)
    router._record_cost(0.05)
    assert router.today_cost_usd() == 0.15
    # a "restart" is just a fresh read of the file
    assert router.today_cost_usd() == 0.15


def test_budget_exhausted_blocks_calls_before_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "COST_LEDGER_PATH", tmp_path / "cost_ledger.json")
    monkeypatch.setattr(router, "daily_budget_usd", lambda: 0.10)
    router._record_cost(0.10)  # at the cap

    dispatched = []
    monkeypatch.setattr(router, "_dispatch", lambda *a, **k: dispatched.append(1))

    import pytest

    with pytest.raises(router.ModelProviderError, match="budget exhausted"):
        router.call(prompt="hi", tier="cheap")
    assert dispatched == []  # blocked before any provider was contacted


def test_free_calls_never_touch_the_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "COST_LEDGER_PATH", tmp_path / "cost_ledger.json")
    router._record_cost(0.0)
    assert not (tmp_path / "cost_ledger.json").exists()


def test_budget_alert_fires_once_per_day_at_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "COST_LEDGER_PATH", tmp_path / "cost_ledger.json")
    monkeypatch.setattr(router, "daily_budget_usd", lambda: 1.00)
    monkeypatch.setattr(router, "budget_alert_threshold_pct", lambda: 80.0)

    router._record_cost(0.50)
    assert router.budget_alert_due() is False  # 50% — below threshold

    router._record_cost(0.35)
    assert router.budget_alert_due() is True   # 85% — first crossing alerts
    assert router.budget_alert_due() is False  # same day: never twice
    # "restart" (fresh reads) must not re-alert either — the marker is in the file
    assert router.budget_alert_due() is False


def test_provider_token_quota_alerts_once_at_80pct(monkeypatch, tmp_path):
    """The Groq free tier ran dry live with zero warning — usage is now
    tracked per provider per day, with one warning at 80%."""
    monkeypatch.setattr(router, "COST_LEDGER_PATH", tmp_path / "ledger.json")
    router._record_tokens("groq", router.Usage(input_tokens=100_000, output_tokens=50_000))
    assert router.quota_alert_due() == ""  # 150k of 200k = 75%, quiet

    router._record_tokens("groq", router.Usage(input_tokens=20_000, output_tokens=0))
    warning = router.quota_alert_due()    # 170k = 85% — one warning
    assert "groq" in warning and "85%" in warning
    assert router.quota_alert_due() == ""  # never twice in a day
    assert router.provider_tokens_today("groq") == 170_000


def test_cached_prompt_tokens_bill_at_the_discount_rate():
    """The agent prompt keeps its static prefix byte-stable precisely so
    OpenAI's prefix cache bills most input at ~90% off — the ledger must
    reflect that, not overcharge at the full rate."""
    tier = {"pricing": {"input_per_million": 0.20, "output_per_million": 1.25,
                        "cached_input_per_million": 0.02}}
    usage = router.Usage(input_tokens=2000, output_tokens=100, cached_tokens=1500)
    cost = router._cost_usd(tier, usage)
    expected = (500 / 1e6) * 0.20 + (1500 / 1e6) * 0.02 + (100 / 1e6) * 1.25
    assert abs(cost - expected) < 1e-9

    # No cached price configured -> cached tokens bill at full rate
    # (never under-count).
    tier_nocache = {"pricing": {"input_per_million": 0.20, "output_per_million": 1.25}}
    full = router._cost_usd(tier_nocache, usage)
    assert abs(full - ((2000 / 1e6) * 0.20 + (100 / 1e6) * 1.25)) < 1e-9


def test_token_guard_blocks_runaway_prompts_and_warns_once(monkeypatch, tmp_path):
    """The dedicated per-call token guard: normal prompts pass untouched
    (nothing is ever trimmed), an unusually heavy call logs one warning a
    day, and a runaway prompt is refused loudly instead of billed
    silently."""
    import pytest
    from kyraan.control_plane import logging_setup

    monkeypatch.setattr(router, "COST_LEDGER_PATH", tmp_path / "ledger.json")

    router._token_guard("small prompt", "small system", "frontier")  # passes silently

    heavy = "x" * (9_000 * 4)
    router._token_guard(heavy, "", "frontier")   # first heavy call: warns
    router._token_guard(heavy, "", "frontier")   # same day: silent
    log = logging_setup.EVENT_LOG.read_text()
    assert log.count("token_guard_warn") == 1

    runaway = "x" * (25_000 * 4)
    with pytest.raises(router.ModelProviderError, match="token"):
        router._token_guard(runaway, "", "frontier")
    assert "token_guard_blocked" in logging_setup.EVENT_LOG.read_text()


def test_usage_summary_rolls_up_model_calls_by_local_day(monkeypatch, tmp_path):
    """The usage.report tool's aggregator: per-day tokens/calls/cost from
    the model_call audit trail (rotated files included), plus the live
    budget picture."""
    import json as j
    from kyraan.control_plane import logging_setup
    from kyraan.model_router import usage_report

    log = tmp_path / "events.jsonl"
    old = tmp_path / "events-20260820T000000Z.jsonl"
    monkeypatch.setattr(logging_setup, "EVENT_LOG", log)
    monkeypatch.setattr(router, "COST_LEDGER_PATH", tmp_path / "ledger.json")

    from kyraan.control_plane.dnd import local_now
    today = local_now().date().isoformat()
    def call(ts, inp, out, cost, model="gpt-5.4-nano"):
        return j.dumps({"ts": ts, "kind": "model_call", "model": model, "tier": "frontier",
                        "input_tokens": inp, "output_tokens": out, "cached_tokens": 0,
                        "cost_usd": cost})
    log.write_text("\n".join([
        call(f"{today}T04:00:00+00:00", 1000, 100, 0.001),
        call(f"{today}T05:00:00+00:00", 2000, 200, 0.002),
        j.dumps({"ts": f"{today}T05:00:01+00:00", "kind": "intent_classified"}),  # ignored
    ]))
    old.write_text(call(f"{today}T03:00:00+00:00", 500, 50, 0.0005, model="qwen3:8b"))

    summary = usage_report.usage_summary(days=7)
    today_row = next(r for r in summary["days"] if r["date"] == today)
    assert today_row["calls"] == 3                      # rotated file included
    assert today_row["input_tokens"] == 3500
    assert today_row["output_tokens"] == 350
    assert abs(today_row["cost_usd"] - 0.0035) < 1e-9
    assert today_row["by_model"] == {"gpt-5.4-nano": 2, "qwen3:8b": 1}
    assert summary["budget"]["daily_budget_usd"] > 0
