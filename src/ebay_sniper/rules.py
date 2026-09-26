"""Rules on title, condition and price: drop, flag or pass, with reasons.

Keywords are matched case- and accent-insensitively on whole words ("defekt"
does not match "defektfrei"); a trailing ``*`` matches word prefixes ("rott*"
matches "rotto", "rotta", "rotte"). Punctuation is ignored.

Missing the item is much worse than a useless notification, so the rules lean
towards notifying:

- a drop keyword preceded by a negation ("no cracked crystal", "kein Glas
  gebrochen", "senza vetro rotto") only flags the listing;
- a total that cannot be compared with the cap (unknown currency) is flagged,
  not dropped;
- the seller's full description is not checked, only the title and the
  condition notes: descriptions are full of boilerplate.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from ebay_sniper.config import PriceConfig, RulesConfig
from ebay_sniper.models import CurrencyConverter, Listing, Money, Verdict

# Words that negate a drop keyword when they appear shortly before it.
NEGATIONS = frozenset(
    {
        # English
        "no",
        "not",
        "never",
        "without",
        # Italian
        "non",
        "senza",
        "nessun",
        "nessuna",
        "niente",
        # German
        "kein",
        "keine",
        "keinen",
        "keiner",
        "nicht",
        "ohne",
        # French
        "pas",
        "sans",
        "aucun",
        "aucune",
        # Spanish
        "sin",
        "ningun",
        "ninguna",
        "nunca",
    }
)
NEGATION_WINDOW = 3

_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase ASCII words separated by single spaces: accents and punctuation go."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_WORD.findall(stripped))


@dataclass(frozen=True, slots=True)
class Keyword:
    text: str
    pattern: re.Pattern[str]

    @classmethod
    def compile(cls, keyword: str) -> Keyword:
        prefix = keyword.strip().endswith("*")
        words = normalize(keyword.strip().rstrip("*"))
        if not words:
            raise ValueError(f"keyword without letters or digits: {keyword!r}")
        end = "" if prefix else r"(?![a-z0-9])"
        return cls(keyword.strip(), re.compile(rf"(?<![a-z0-9]){re.escape(words)}{end}"))

    def positions(self, normalized: str) -> list[int]:
        return [match.start() for match in self.pattern.finditer(normalized)]


@dataclass(frozen=True, slots=True)
class RuleResult:
    verdict: Verdict
    reasons: tuple[str, ...] = ()


class RuleEngine:
    def __init__(self, rules: RulesConfig, price: PriceConfig) -> None:
        self._drop = _compile_all(rules.drop_keywords)
        self._flag = _compile_all(rules.flag_keywords)
        self._flag_conditions = {str(condition) for condition in rules.flag_condition_ids}
        self._converter = CurrencyConverter(price.currency, price.exchange_rates)
        self._max_total = Money(price.max_total, price.currency)

    @property
    def converter(self) -> CurrencyConverter:
        return self._converter

    def evaluate(self, listing: Listing) -> RuleResult:
        drops: list[str] = []
        flags: list[str] = []
        texts = [("title", listing.title)]
        if listing.condition_description:
            texts.append(("condition notes", listing.condition_description))
        for where, text in texts:
            normalized = normalize(text)
            for keyword in self._drop:
                positions = keyword.positions(normalized)
                if not positions:
                    continue
                if all(_is_negated(normalized, position) for position in positions):
                    flags.append(f"negated '{keyword.text}' in {where}")
                else:
                    drops.append(f"'{keyword.text}' in {where}")
            flags.extend(
                f"'{keyword.text}' in {where}"
                for keyword in self._flag
                if keyword.positions(normalized)
            )
        if listing.condition_id in self._flag_conditions:
            flags.append(f"condition {listing.condition_id} ({listing.condition or 'unknown'})")
        self._check_price(listing, drops, flags)
        verdict = Verdict.DROP if drops else Verdict.FLAG if flags else Verdict.PASS
        return RuleResult(verdict, tuple(drops + flags))

    def _check_price(self, listing: Listing, drops: list[str], flags: list[str]) -> None:
        total = listing.total
        if total is None:
            return
        converted = self._converter.convert(total)
        if converted is None:
            flags.append(f"no exchange rate for {total.currency}, price cap not checked")
        elif converted.value > self._max_total.value:
            shown = str(total) if converted is total else f"{total} (about {converted})"
            drops.append(f"total {shown} above the cap of {self._max_total}")


def _compile_all(keywords: Iterable[str]) -> list[Keyword]:
    return [Keyword.compile(keyword) for keyword in keywords]


def _is_negated(normalized: str, position: int) -> bool:
    preceding = normalized[:position].split()[-NEGATION_WINDOW:]
    return any(word in NEGATIONS for word in preceding)
