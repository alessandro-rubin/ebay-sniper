from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from ebay_sniper.config import AppConfig, PriceConfig, RulesConfig, load_config
from ebay_sniper.models import Listing, Money, Verdict
from ebay_sniper.rules import Keyword, RuleEngine, normalize

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_listing(title: str = "Futura Quartz spider web watch", **changes: object) -> Listing:
    listing = Listing(
        listing_id="1",
        item_id="v1|1|0",
        title=title,
        url="https://www.ebay.it/itm/1",
        marketplace="EBAY_IT",
        query="q",
        price=Money(Decimal("25.00"), "EUR"),
        shipping=Money(Decimal("5.00"), "EUR"),
        buying_options=("FIXED_PRICE",),
    )
    return replace(listing, **changes)


def engine(
    drop: list[str] | None = None,
    flag: list[str] | None = None,
    conditions: list[int] | None = None,
    max_total: str = "100",
    rates: dict[str, Decimal] | None = None,
) -> RuleEngine:
    return RuleEngine(
        RulesConfig(
            drop_keywords=drop or [],
            flag_keywords=flag or [],
            flag_condition_ids=conditions or [],
        ),
        PriceConfig(max_total=Decimal(max_total), exchange_rates=rates or {}),
    )


@pytest.fixture(scope="module")
def repo_rules() -> RuleEngine:
    config: AppConfig = load_config(REPO_ROOT / "config.toml")
    return RuleEngine(config.rules, config.price)


def test_normalize() -> None:
    assert normalize("Verre CASS\u00c9 !!") == "verre casse"
    assert normalize("Glas-gebrochen,  Zeiger fehlt.") == "glas gebrochen zeiger fehlt"
    assert normalize("Gro\u00dfe Uhr") == "grosse uhr"
    assert normalize("  ") == ""


def test_keywords_match_whole_words_or_prefixes() -> None:
    defekt = Keyword.compile("defekt")
    assert defekt.positions(normalize("Uhr defekt"))
    assert not defekt.positions(normalize("Uhr defektfrei"))
    prefix = Keyword.compile("missing hand*")
    assert prefix.positions(normalize("Missing hands!"))
    assert not prefix.positions(normalize("missing the hand"))
    assert Keyword.compile("verre cass\u00e9").positions(normalize("VERRE CASSE"))
    with pytest.raises(ValueError, match="without letters"):
        Keyword.compile(" * ")


def test_drop_keyword_in_title() -> None:
    result = engine(drop=["cracked crystal"]).evaluate(make_listing("Watch, cracked crystal"))
    assert result.verdict is Verdict.DROP
    assert result.reasons == ("'cracked crystal' in title",)


def test_negated_drop_keyword_only_flags() -> None:
    listing = make_listing(condition_description="Perfetto, senza vetro rotto o graffi.")
    result = engine(drop=["vetro rotto"]).evaluate(listing)
    assert result.verdict is Verdict.FLAG
    assert result.reasons == ("negated 'vetro rotto' in condition notes",)


def test_one_plain_occurrence_is_enough_to_drop() -> None:
    listing = make_listing(
        condition_description="No cracked crystal on the front. Back: cracked crystal."
    )
    assert engine(drop=["cracked crystal"]).evaluate(listing).verdict is Verdict.DROP


def test_negation_further_away_does_not_count() -> None:
    listing = make_listing(condition_description="No box. Watch with cracked crystal.")
    assert engine(drop=["cracked crystal"]).evaluate(listing).verdict is Verdict.DROP


def test_flags_and_condition_ids() -> None:
    listing = make_listing(
        "Spider watch needs battery", condition_id="7000", condition="For parts or not working"
    )
    result = engine(flag=["needs battery"], conditions=[7000]).evaluate(listing)
    assert result.verdict is Verdict.FLAG
    assert result.reasons == (
        "'needs battery' in title",
        "condition 7000 (For parts or not working)",
    )


def test_drop_wins_over_flags_and_keeps_all_reasons() -> None:
    listing = make_listing("Cracked crystal, needs battery")
    result = engine(drop=["cracked crystal"], flag=["needs battery"]).evaluate(listing)
    assert result.verdict is Verdict.DROP
    assert result.reasons == ("'cracked crystal' in title", "'needs battery' in title")


def test_clean_listing_passes() -> None:
    result = engine(drop=["cracked crystal"], flag=["defekt"]).evaluate(make_listing())
    assert (result.verdict, result.reasons) == (Verdict.PASS, ())


def test_price_cap_in_the_same_currency() -> None:
    rules = engine(max_total="30")
    assert rules.evaluate(make_listing()).verdict is Verdict.PASS  # 25 + 5 = 30
    expensive = make_listing(price=Money(Decimal("25.01"), "EUR"))
    result = rules.evaluate(expensive)
    assert result.verdict is Verdict.DROP
    assert result.reasons == ("total 30.01 EUR above the cap of 30.00 EUR",)


def test_price_cap_with_conversion() -> None:
    rules = engine(max_total="100", rates={"USD": Decimal("0.86")})
    cheap = make_listing(price=Money(Decimal("100"), "USD"), shipping=Money(Decimal("10"), "USD"))
    assert rules.evaluate(cheap).verdict is Verdict.PASS  # 110 USD = 94.60 EUR
    dear = make_listing(price=Money(Decimal("120"), "USD"), shipping=None)
    result = rules.evaluate(dear)
    assert result.verdict is Verdict.DROP
    assert result.reasons == ("total 120.00 USD (about 103.20 EUR) above the cap of 100.00 EUR",)


def test_unknown_currency_is_flagged_not_dropped() -> None:
    listing = make_listing(price=Money(Decimal("9999"), "GBP"), shipping=None)
    result = engine(max_total="100").evaluate(listing)
    assert result.verdict is Verdict.FLAG
    assert result.reasons == ("no exchange rate for GBP, price cap not checked",)


def test_auction_cap_uses_the_current_bid() -> None:
    auction = make_listing(
        buying_options=("AUCTION", "FIXED_PRICE"),
        price=Money(Decimal("500"), "EUR"),  # Buy It Now
        current_bid=Money(Decimal("40"), "EUR"),
    )
    assert engine(max_total="100").evaluate(auction).verdict is Verdict.PASS


def test_unknown_price_is_not_dropped() -> None:
    assert engine(max_total="1").evaluate(make_listing(price=None)).verdict is Verdict.PASS


@pytest.mark.parametrize(
    ("title", "verdict"),
    [
        # Drops, one per language.
        ("Orologio Futura quadrante ragnatela, vetro rotto", Verdict.DROP),
        ("Futura spider web watch - cracked crystal", Verdict.DROP),
        ("Damenuhr Spinnennetz, Glas gebrochen", Verdict.DROP),
        ("Montre araign\u00e9e, verre cass\u00e9", Verdict.DROP),
        ("Reloj ara\u00f1a, cristal roto", Verdict.DROP),
        ("Spider watch missing hands", Verdict.DROP),
        # Flags, one per language.
        ("Orologio ragnatela non funzionante", Verdict.FLAG),
        ("Spider web watch for parts or repair", Verdict.FLAG),
        ("Spinnennetz Armbanduhr defekt", Verdict.FLAG),
        ("Montre toile d'araign\u00e9e ne fonctionne pas", Verdict.FLAG),
        ("Reloj telara\u00f1a no funciona", Verdict.FLAG),
        ("Uhr f\u00fcr Bastler", Verdict.FLAG),
        # Clean titles, including near misses.
        ("Vintage Futura Quartz Spider Nest MOP watch", Verdict.PASS),
        ("Spinnennetz Uhr, Glas ohne Kratzer", Verdict.PASS),
        # Prefix keywords over-flag a little; a warning is cheap.
        ("Defekte Spinnennetz Uhr", Verdict.FLAG),
        ("Spinnennetz Uhr defektfrei", Verdict.FLAG),
        ("Orologio donna ragnatela funzionante", Verdict.PASS),
    ],
)
def test_repository_rules_in_five_languages(
    repo_rules: RuleEngine, title: str, verdict: Verdict
) -> None:
    result = repo_rules.evaluate(make_listing(title))
    assert result.verdict is verdict, result.reasons


def test_repository_rules_on_condition_notes(repo_rules: RuleEngine) -> None:
    fine = make_listing(condition_description="Funziona, nessun vetro rotto, cinturino nuovo.")
    assert repo_rules.evaluate(fine).verdict is Verdict.FLAG
    broken = make_listing(condition_description="Il vetro rotto va sostituito.")
    assert repo_rules.evaluate(broken).verdict is Verdict.DROP
