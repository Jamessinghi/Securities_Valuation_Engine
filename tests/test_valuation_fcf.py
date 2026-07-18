from app.valuation.methods import Ctx


def test_statement_capex_is_normalised_and_fx_converted():
    ctx = Ctx(
        {
            "op_cash_flow": {"value": 2_813},
            "capex": {"value": -2_056},
            "free_cash_flow": {"value": 1_777},
        },
        market={},
        reporting_currency="USD",
        fx=1.5,
    )

    assert ctx.capex == 3_084
    assert ctx.free_cash_flow() == 1_135.5
    assert "operating cash flow − CapEx" in ctx.fcf_note()
    assert "company-reported FCF 2,666" in ctx.fcf_note()


def test_reported_fcf_is_fallback_when_operating_cash_flow_is_missing():
    ctx = Ctx(
        {"free_cash_flow": {"value": 450}},
        market={},
        reporting_currency="USD",
        fx=1.5,
    )

    assert ctx.free_cash_flow() == 675
    assert ctx.fcf_note() == "company-reported free cash flow"


def test_dna_is_flagged_as_fallback_when_capex_is_missing():
    ctx = Ctx(
        {
            "op_cash_flow": {"value": 900},
            "dna": {"value": -250},
        },
        market={},
    )

    assert ctx.free_cash_flow() == 650
    assert "CapEx not extracted; proxy" in ctx.fcf_note()


def test_implausible_capex_is_rejected_before_fcf_calculation():
    ctx = Ctx(
        {
            "op_cash_flow": {"value": 100},
            "capex": {"value": -500},
            "dna": {"value": 20},
        },
        market={},
    )

    assert ctx.capex is None
    assert ctx.free_cash_flow() == 80
