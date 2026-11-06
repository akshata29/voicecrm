"""In-process index over the product catalogue.

Matches spoken/written fund names to catalogue *families* (a fund with all its share classes) using idf-weighted token
overlap - exact names, brand-less names ("the equity fund"), aliases (EN + ES) and speech-recogniser slips ("Frankland
Equity Funds") - and finds share-class phrases ("class C", "clase C", "R6", "advisor class"). Share-class detection is
kept separate from fund detection so that "Class C, not Class A" resolves to the right product.

`LocalCatalogueSearch` exposes the same index through the CatalogueSearchPort (used by the UI product picker and as the
offline stand-in for Azure AI Search).
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from rapidfuzz import fuzz, process

from ..domain.catalogue import Catalogue, Product
from ..ports.search import SearchHit
from .text import Tok, norm_words, tokenize

BRANDS = frozenset({"contoso", "woodgrove"})
FUND_WORDS = frozenset({"fund", "funds", "fondo", "fondos", "etf"})
_TRAILING_FUND_WORDS = frozenset({"fund", "funds", "etf"})
GENERIC = frozenset({
    "fund", "funds", "fondo", "fondos", "etf", "the", "of", "de", "la", "el", "los", "las", "del", "and", "y", "a", "an",
    "our", "your", "their", "this", "that", "new", "trust", "con", "para", "en", "por", "sin", "al", "e", "o", "or", "in",
})
_BRIDGES = frozenset({"de", "of", "del"})
# speech-recogniser slips (see adapters/transcription/noise.py) that are cheaper to map than to fuzzy-match
NOISE_MAP = {
    "fun": "fund", "funs": "funds", "equities": "equity", "equitys": "equity", "adviser": "advisor", "advisers": "advisors",
    "contosa": "contoso", "kontoso": "contoso", "contosos": "contoso", "woodgroves": "woodgrove",
}
CLASS_PRIOR = {
    "professional_buyer": ["I", "R6", "Advisor", "A", "C", "R", "W", "ETF"],
    "retail": ["A", "C", "Advisor", "R6", "I", "R", "W", "ETF"],
    "international": ["I", "W", "A", "R6", "Advisor", "C", "R", "ETF"],
}
_ALIAS_STOP = frozenset({"fondo", "fondos"})


class ClassVariant(NamedTuple):
    tokens: tuple[str, ...]
    cls: str
    weak: bool                           # single letter + "class"/"share": only trusted right next to a fund name


@dataclass(slots=True)
class Family:
    family_id: str
    fund_name: str
    brand: str | None
    name_tokens: tuple[str, ...]
    core_sets: list[tuple[str, ...]]     # [0] = official name, then aliases
    aliases: list[str] = field(default_factory=list)
    products: dict[str, Product] = field(default_factory=dict)
    vehicle: str = "Mutual Fund"
    asset_class: str = ""


@dataclass(slots=True)
class FamilyMatch:
    family: Family
    score: float
    exact: bool
    brand_mismatch: bool = False
    via_alias: bool = False


@dataclass(slots=True)
class FundSpan:
    start: int                           # token indexes into the segment's token list, end exclusive
    end: int
    tokens: list[str]                    # canonicalised words of the span
    has_brand: bool
    has_fund_word: bool

    @property
    def core(self) -> tuple[str, ...]:
        return _core(self.tokens)


@dataclass(slots=True)
class ClassHit:
    start: int
    end: int
    cls: str
    weak: bool


def _core(tokens: Sequence[str]) -> tuple[str, ...]:
    return tuple(t for t in tokens if t not in BRANDS and t not in GENERIC)


class CatalogueIndex:
    def __init__(self, catalogue: Catalogue) -> None:
        self.catalogue = catalogue
        self.families: dict[str, Family] = {}
        self.inverted: dict[str, set[str]] = {}
        self.idf: dict[str, float] = {}
        self.class_variants: list[ClassVariant] = []
        self._canon_cache: dict[str, str | None] = {}
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        for p in self.catalogue.products:
            fam = self.families.get(p.family_id)
            if fam is None:
                name_tokens = tuple(norm_words(p.fund_name))
                brand = next((t for t in name_tokens if t in BRANDS), None)
                fam = Family(p.family_id, p.fund_name, brand, name_tokens, [_core(name_tokens)], vehicle=p.vehicle,
                             asset_class=p.asset_class)
                self.families[p.family_id] = fam
            fam.products[p.share_class] = p
            for alias in p.aliases:
                if alias not in fam.aliases:
                    fam.aliases.append(alias)
                    core = _core([t for t in norm_words(alias) if t not in _ALIAS_STOP])
                    if core and core not in fam.core_sets:
                        fam.core_sets.append(core)
        for fid, fam in self.families.items():
            for core in fam.core_sets:
                for tok in core:
                    self.inverted.setdefault(tok, set()).add(fid)
        n = max(len(self.families), 1)
        self.idf = {tok: math.log(1.0 + n / len(fids)) for tok, fids in self.inverted.items()}
        self.vocab = set(self.inverted) | BRANDS | FUND_WORDS
        self._vocab_list = sorted(self.vocab)
        variants: list[ClassVariant] = []
        for cls, phrases in self.catalogue.share_classes.items():
            for phrase in phrases:
                toks = tuple(norm_words(phrase))
                if toks:
                    weak = len(toks) == 2 and len(toks[0]) == 1 and toks[1] in {"class", "share", "clase"}
                    variants.append(ClassVariant(toks, cls, weak))
        self.class_variants = sorted(variants, key=lambda v: -len(v.tokens))

    # ------------------------------------------------------------------ token canonicalisation
    def canon(self, token: str) -> str | None:
        """Maps a (possibly mis-recognised) token onto the catalogue vocabulary, or None when it is not part of it."""
        if token in self.vocab:
            return token
        if token in self._canon_cache:
            return self._canon_cache[token]
        mapped = NOISE_MAP.get(token)
        if mapped is None and len(token) >= 5:
            hit = process.extractOne(token, self._vocab_list, scorer=fuzz.ratio, score_cutoff=87)
            mapped = hit[0] if hit else None
        mapped = mapped if mapped in self.vocab else None
        self._canon_cache[token] = mapped
        return mapped

    # ------------------------------------------------------------------ fund spans
    def find_fund_spans(self, toks: list[Tok]) -> list[FundSpan]:
        """Maximal runs of catalogue-vocabulary tokens that look like a fund name (brand or 'fund' word, plus a core term)."""
        words: list[str] = []
        back: list[int] = []                                   # compact word index -> original token index
        for k, t in enumerate(toks):
            if t.norm == "grove" and words and words[-1] == "wood":        # recogniser split "Wood grove"
                words[-1] = "woodgrove"
                continue
            words.append(t.norm)
            back.append(k)
        spans: list[FundSpan] = []
        n, i = len(words), 0
        while i < n:
            if self.canon(words[i]) is None:
                i += 1
                continue
            j, seq, brand_seen = i, [], False
            while j < n:
                c = self.canon(words[j])
                if c is None:
                    if words[j] in _BRIDGES and seq and j + 1 < n and self.canon(words[j + 1]) is not None:
                        j += 1
                        continue
                    break
                if c in BRANDS:
                    if brand_seen and any(t in _TRAILING_FUND_WORDS for t in seq):
                        break
                    brand_seen = True
                seq.append(c)
                j += 1
                if c in _TRAILING_FUND_WORDS:
                    break
            has_brand = any(t in BRANDS for t in seq)
            has_fund = any(t in FUND_WORDS for t in seq)
            if _core(seq) and (has_brand or has_fund):
                spans.append(FundSpan(back[i], back[j - 1] + 1, seq, has_brand, has_fund))
            i = max(j, i + 1)
        return self._with_unknown_names(toks, spans)

    def _with_unknown_names(self, toks: list[Tok], spans: list[FundSpan]) -> list[FundSpan]:
        """'Contoso Blue Chip Fund': a brand, capitalised words the catalogue has never seen, then a fund word.

        Legacy names and nicknames are exactly what a reviewer later teaches the system, so they must surface as a mention
        (with no candidate) instead of silently vanishing."""
        out = list(spans)
        n = len(toks)
        for i, t in enumerate(toks):
            if self.canon(t.norm) not in BRANDS or any(s.start <= i < s.end and s.has_brand for s in out):
                continue
            j, unknown, found = i + 1, 0, False
            while j < n and j - i <= 6:
                canon = self.canon(toks[j].norm)
                if canon in _TRAILING_FUND_WORDS:
                    found = True
                    break
                if canon is None and not (toks[j].text[:1].isupper() or toks[j].text[:1].isdigit()):
                    break
                unknown += canon is None
                j += 1
            if not (found and unknown):
                continue
            tokens = [self.canon(x.norm) or x.norm for x in toks[i:j + 1]]
            out = [s for s in out if s.end <= i or s.start > j]
            out.append(FundSpan(i, j + 1, tokens, True, True))
        return sorted(out, key=lambda s: s.start)

    # ------------------------------------------------------------------ families
    def match_families(self, tokens: Sequence[str], *, limit: int = 5) -> list[FamilyMatch]:
        canon = [self.canon(t) or t for t in tokens]
        brand = next((t for t in canon if t in BRANDS), None)
        probe = {t for t in canon if t not in BRANDS and t not in GENERIC and t in self.inverted}
        if not probe:
            return []
        weight_probe = sum(self.idf[t] for t in probe)
        candidates = set().union(*(self.inverted[t] for t in probe))
        out: list[FamilyMatch] = []
        for fid in candidates:
            fam = self.families[fid]
            best, best_exact, best_ci = 0.0, False, 0
            for ci, core in enumerate(fam.core_sets):
                cset = set(core)
                inter = probe & cset
                if not inter:
                    continue
                w_inter = sum(self.idf[t] for t in inter)
                score = 0.5 * (w_inter / weight_probe + w_inter / sum(self.idf[t] for t in cset))
                if ci:
                    score *= 0.98
                if score > best:
                    best, best_exact, best_ci = score, probe == cset, ci
            mismatch = bool(brand and fam.brand and brand != fam.brand)
            if brand and fam.brand:
                best = max(0.0, best - 0.12) if mismatch else min(1.0, best + 0.04)
            if best > 0:
                out.append(FamilyMatch(fam, round(best, 4), best_exact and not mismatch, mismatch, best_ci > 0))
        out.sort(key=lambda m: (-m.score, m.family.family_id))
        return out[:limit]

    # ------------------------------------------------------------------ share classes
    def find_classes(self, toks: list[Tok]) -> list[ClassHit]:
        """Share-class phrases in a token run (longest variant wins; a bare 'a' / 'I' must be written as a capital)."""
        words = [t.norm for t in toks]
        taken: set[int] = set()
        hits: list[ClassHit] = []
        for variant in self.class_variants:
            n = len(variant.tokens)
            for i in range(0, len(words) - n + 1):
                if tuple(words[i:i + n]) != variant.tokens or any(k in taken for k in range(i, i + n)):
                    continue
                if n == 2 and variant.tokens[0] in {"a", "i"} and not toks[i].text.isupper():
                    continue
                if n == 2 and variant.tokens[0] in {"class", "clase"} and variant.tokens[1] in {"a", "i"} \
                        and not toks[i + 1].text.isupper():
                    continue
                taken.update(range(i, i + n))
                hits.append(ClassHit(i, i + n, variant.cls, variant.weak))
        hits.sort(key=lambda h: h.start)
        return hits

    def class_order(self, family: Family, business_line: str) -> list[str]:
        prior = CLASS_PRIOR.get(business_line, CLASS_PRIOR["professional_buyer"])
        ranked = [c for c in prior if c in family.products]
        return ranked + [c for c in family.products if c not in ranked]

    # ------------------------------------------------------------------ phrases for speech recognition
    def phrase_list(self, limit: int = 500) -> list[str]:
        phrases = ["Contoso", "Woodgrove", "share class", "Class A", "Class C", "Class I", "Class R6", "Advisor Class",
                   "R6", "ETF", "UCITS", "Clase A", "Clase C", "Clase I"]
        seen = {p.lower() for p in phrases}
        for fam in self.families.values():
            for name in (fam.fund_name, *fam.aliases):
                if name.lower() not in seen:
                    seen.add(name.lower())
                    phrases.append(name)
                if len(phrases) >= limit:
                    return phrases[:limit]
        return phrases[:limit]


_QUERY_CLASS_WORDS = {"advisor": "Advisor", "adviser": "Advisor", "institutional": "I", "inst": "I", "r6": "R6", "retirement": "R"}
_QUERY_CLASS_LETTERS = {"a": "A", "c": "C", "i": "I", "r": "R", "w": "W"}


class LocalCatalogueSearch:
    """CatalogueSearchPort over the in-process index: family matching plus fuzzy display-name search."""

    name = "local"

    def __init__(self, catalogue: Catalogue | None = None, *, cindex: CatalogueIndex | None = None) -> None:
        if cindex is None and catalogue is None:
            raise ValueError("LocalCatalogueSearch needs a catalogue or an index")
        self.cindex = cindex or CatalogueIndex(catalogue)         # type: ignore[arg-type]
        self._load_names()

    def _load_names(self) -> None:
        self._products = list(self.cindex.catalogue.products)
        self._names = [" ".join(norm_words(p.display_name)) for p in self._products]

    async def index(self, catalogue: Catalogue) -> int:
        self.cindex = CatalogueIndex(catalogue)
        self._load_names()
        return len(catalogue.products)

    async def search(self, query: str, *, top: int = 10, business_line: str | None = None) -> list[SearchHit]:
        toks = tokenize(query)
        words = [t.norm for t in toks]
        scored: dict[str, float] = {}
        by_lower = {p.product_id.lower(): p for p in self._products}
        exact = by_lower.get(query.strip().lower())
        if exact is not None:                      # a pasted product id (SYN-000009) finds exactly that product
            scored[exact.product_id] = 1.5
        if words:
            classes = {h.cls for h in self.cindex.find_classes(toks)}
            for w in words:                       # a person typing a search means the share class, not "an advisor"
                if w in _QUERY_CLASS_WORDS:
                    classes.add(_QUERY_CLASS_WORDS[w])
                elif len(w) == 1 and w in _QUERY_CLASS_LETTERS and len(words) > 1:
                    classes.add(_QUERY_CLASS_LETTERS[w])
            for fm in self.cindex.match_families(words, limit=8):
                order = self.cindex.class_order(fm.family, business_line or "professional_buyer")
                for rank, cls in enumerate(order):
                    boost = (0.25 if cls in classes else -0.2) if classes else -0.01 * rank
                    pid = fm.family.products[cls].product_id
                    scored[pid] = max(scored.get(pid, 0.0), max(0.0, fm.score + boost))
            for _, score, idx in process.extract(" ".join(words), self._names, scorer=fuzz.WRatio, limit=top * 2):
                pid = self._products[idx].product_id
                scored[pid] = max(scored.get(pid, 0.0), score / 100.0 * 0.9)
        by_id = {p.product_id: p for p in self._products}
        hits: list[SearchHit] = []
        for pid, score in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:top]:
            p = by_id[pid]
            hits.append(SearchHit(id=pid, score=round(score, 4), fields={
                "display_name": p.display_name, "fund_name": p.fund_name, "share_class": p.share_class,
                "family_id": p.family_id, "asset_class": p.asset_class, "vehicle": p.vehicle,
                "business_lines": p.business_lines}))
        return hits
