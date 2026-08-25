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
