"""Rules-based product resolver: catalogue-grounded fund detection, share-class linking and top-k candidates.

`detect_mentions` finds fund names in the policy-filtered segments, links a share-class phrase to each ("class C",
"clase C", "R6") while respecting negation ("... not class A"), and tags how the speaker relates to the product
(interest / existing holding / comparison / objection / info only). `resolve` turns those raw mentions into catalogue
products: an exact product when the share class was stated, otherwise the fund's classes as ranked candidates that a
human picks from. It never guesses a share class.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from ..domain.catalogue import Catalogue, LearnedAlias
from ..domain.enums import MentionType, SpeakerRole
from ..domain.extraction import Evidence, ProductCandidate, ProductMention
from .catalogue_index import CatalogueIndex, ClassHit, Family, FamilyMatch, FundSpan
from .contracts import PolicySegment, RawProductMention
from .text import Tok, norm_words, normalize_surface, sentence_spans, tokenize

_LINK_WORDS = frozenset({"of", "in", "from", "for", "on", "de", "del", "the", "la", "el", "los", "las", "shares", "share",
                         "acciones", "with"})
_NEGATORS = frozenset({
    "not", "no", "nor", "never", "without", "instead", "rather", "versus", "vs", "than", "isnt", "arent", "wasnt", "werent",
    "dont", "nunca", "ni", "sin", "unlike", "opposed", "lugar", "vez",
})
_CORRECTION = re.compile(r"\b(?:actually|correction|i meant|to clarify|to be clear|sorry|in fact|hold on|wait|en realidad|perd[oó]n|"
                         r"quise decir)\b|^\s*no[,.]", re.I)
_PRIORITY = [MentionType.objection, MentionType.existing_holding, MentionType.comparison, MentionType.interest]


def _phrases(*texts: str) -> list[tuple[str, ...]]:
    return [tuple(norm_words(t)) for t in texts]


_LEADING: dict[MentionType, list[tuple[str, ...]]] = {
    MentionType.objection: _phrases(
        "concerned about", "concern about", "concerns about", "worried about", "not comfortable with", "dont like",
        "unhappy with", "disappointed with", "problem with", "pushback on", "cant justify", "no me gusta",
        "me preocupa", "nos preocupa", "preocupados por"),
    MentionType.existing_holding: _phrases(
        "currently hold", "currently own", "currently use", "already hold", "already own", "already use", "we hold",
        "we own", "i hold", "we use", "invested in", "our position in", "existing position in", "existing holding in",
        "holding the", "owns the", "we are in", "tenemos", "ya tenemos", "actualmente tenemos", "mantenemos",
        "estamos invertidos en", "ya invertimos en", "ya usamos"),
    MentionType.comparison: _phrases(
        "compared to", "compared with", "compare to", "compare with", "compares to", "compares with", "versus", "vs",
        "alternative to", "instead of", "benchmark against", "side by side with", "stack up against", "comparado con",
        "frente a", "en lugar de", "alternativa a", "comparar con"),
    MentionType.interest: _phrases(
        "interested in", "interest in", "looking at", "looking to", "looking into", "considering", "want to", "wants to",
        "would like", "wed like", "thinking about", "thinking of", "evaluating", "evaluate", "exploring", "plan to",
        "planning to", "adding", "add the", "allocate to", "allocating to", "buy", "invest in", "investing in",
        "move into", "switch to", "convert to", "move to", "recommend", "suggest", "propose", "pitch", "me interesa",
        "nos interesa", "estamos interesados en", "interesados en", "queremos", "evaluando", "considerando",
        "nos gustaria", "quisieramos", "pensando en", "quiere"),
}
_TRAILING: dict[MentionType, list[tuple[str, ...]]] = {
    MentionType.objection: _phrases(
        "too expensive", "too high", "too costly", "overpriced", "isnt competitive", "is a concern", "worries us",
        "demasiado caro", "es muy caro"),
    MentionType.existing_holding: _phrases(
        "already in the portfolio", "in the portfolio today", "is our core holding", "ya esta en la cartera"),
    MentionType.interest: _phrases(
        "looks interesting", "sounds interesting", "looks attractive", "looks good", "is a good fit", "would fit",
        "nos interesa", "suena interesante"),
}


def _nearest(window: list[str], cues: dict[MentionType, list[tuple[str, ...]]], *, leading: bool) -> MentionType | None:
    best: tuple[int, int, MentionType] | None = None
    for rank, typ in enumerate(_PRIORITY):
        for phrase in cues.get(typ, []):
            n = len(phrase)
            for i in range(0, len(window) - n + 1):
                if tuple(window[i:i + n]) == phrase:
                    gap = len(window) - (i + n) if leading else i
                    cand = (gap, rank, typ)
                    if best is None or cand[:2] < best[:2]:
                        best = cand
    return best[2] if best else None


@dataclass(slots=True)
class _Decision:
    matches: list[FamilyMatch]
    confidence: float
    ambiguous: bool
    specific: bool
    fuzzy: bool
    has_brand: bool = False
    forced_product: str | None = None
    key: str = ""


@dataclass(slots=True)
class _Group:
    key: str
    raws: list[RawProductMention] = field(default_factory=list)
    decisions: list[_Decision] = field(default_factory=list)
    first_segment: int = 0


class RulesProductResolver:
    def __init__(self, catalogue: Catalogue, *, index: CatalogueIndex | None = None) -> None:
        self.catalogue = catalogue
        self.index = index or CatalogueIndex(catalogue)
        self._products = catalogue.by_id()
        self._learned: dict[str, tuple[str, int]] = {}

    # ------------------------------------------------------------------ aliases / phrases
    def reload_aliases(self, aliases: list[LearnedAlias]) -> None:
        learned: dict[str, tuple[str, int]] = {}
        for a in aliases:
            key = normalize_surface(a.surface)
            if key and a.product_id in self._products and (key not in learned or a.count >= learned[key][1]):
                learned[key] = (a.product_id, a.count)
        self._learned = learned

    def phrase_list(self, limit: int = 500) -> list[str]:
        return self.index.phrase_list(limit)

    # ------------------------------------------------------------------ detection
    def detect_mentions(self, segments: list[PolicySegment], *, language: str) -> list[RawProductMention]:
        raws: list[RawProductMention] = []
        usable = [s for s in segments if s.text.strip() and s.label != "sensitive"]
        for pos, seg in enumerate(usable):
            toks = tokenize(seg.text)
            spans = self.index.find_fund_spans(toks)
            if not spans:
                continue
            assigned, leftovers = self._assign_classes(toks, spans, self.index.find_classes(toks))
            sentences = sentence_spans(seg.text)
            for si, span in enumerate(spans):
                links = assigned.get(si, [])
                positives: list[ClassHit] = []
                for hit, negated in links:
                    if not negated and hit.cls not in {h.cls for h in positives}:
                        positives.append(hit)
                negated_here = sorted({h.cls for h, negated in links if negated} | {h.cls for h, near in leftovers if near == si})
                extra_ids: list[int] = []
                if not positives:
                    ahead = self._class_in_following(usable, pos)
                    if ahead is not None:
                        positives, extra_ids = [ahead[0]], [ahead[1]]
                stance, explicit = self._stance(toks, span, seg.text, sentences, seg.speaker_role)
                specific = self._is_specific(span)
                quote = self._quote(seg.text, toks, span, positives, sentences)
                corrects = bool(_CORRECTION.search(quote))
                variants: list[ClassHit | None] = list(positives) or [None]
                for hit in variants:
                    surface = self._surface(seg.text, toks, span, hit, extra_ids)
                    raws.append(RawProductMention(
                        surface=surface, segment_ids=[seg.id, *extra_ids], quote=quote,
                        share_class_stated=hit.cls if hit else None,
                        mention_type=MentionType.comparison if len(variants) > 1 and not explicit else stance,
                        explicit=explicit or len(variants) > 1, branded=specific, negated_classes=negated_here, corrects=corrects))
        return raws

    @staticmethod
    def _is_specific(span: FundSpan) -> bool:
        firm_only = "contoso" in span.tokens and "woodgrove" in span.tokens
        return (span.has_brand and not firm_only) or len(span.core) >= 2

    def _assign_classes(self, toks: list[Tok], spans: list[FundSpan], hits: list[ClassHit]
                        ) -> tuple[dict[int, list[tuple[ClassHit, bool]]], list[tuple[ClassHit, int]]]:
        """Links each share-class phrase to one fund name; negated phrases that link to nothing go to the nearest fund."""
        assigned: dict[int, list[tuple[ClassHit, bool]]] = {i: [] for i in range(len(spans))}
        leftovers: list[tuple[ClassHit, int]] = []
        for h in hits:
            if h.cls == "ETF" or any(h.start < sp.end and h.end > sp.start for sp in spans):   # "ETF" is a vehicle, not a class
                continue
            options: list[tuple[int, int, int]] = []
            for si, sp in enumerate(spans):
                if h.start >= sp.end:
                    gap = h.start - sp.end
                    if (h.weak and gap > 0) or gap > 6:
                        continue
                    if any(o.start >= sp.end and o.end <= h.start for o in spans):
                        continue
                    options.append((0, gap, si))
                else:
                    between = toks[h.end:sp.start]
                    if (h.weak and between) or len(between) > 3 or any(t.norm not in _LINK_WORDS for t in between):
                        continue
                    if any(o.end <= sp.start and o.start >= h.end for o in spans):
                        continue
                    options.append((1, len(between), si))
            negated = self._negated(toks, h)
            if options:
                assigned[min(options)[2]].append((h, negated))
            elif negated and spans:
                nearest = min(range(len(spans)), key=lambda i: min(abs(h.start - spans[i].end), abs(spans[i].start - h.end)))
                leftovers.append((h, nearest))
        return assigned, leftovers

    @staticmethod
    def _negated(toks: list[Tok], hit: ClassHit) -> bool:
        return any(t.norm in _NEGATORS for t in toks[max(0, hit.start - 3):hit.start])

    def _class_in_following(self, usable: list[PolicySegment], pos: int) -> tuple[ClassHit, int] | None:
        """'Which share class?' -> 'Class C.' : a class phrase at the start of the next two segments, with no other fund named."""
        for seg in usable[pos + 1: pos + 3]:
            toks = tokenize(seg.text)
            if self.index.find_fund_spans(toks):
                return None
            for h in self.index.find_classes(toks):
                if not h.weak and h.cls != "ETF" and not self._negated(toks, h) and h.start <= 8:
                    return h, seg.id
        return None

    # ------------------------------------------------------------------ stance and text
    def _stance(self, toks: list[Tok], span: FundSpan, text: str, sentences: list[tuple[int, int]],
                role: SpeakerRole) -> tuple[MentionType, bool]:
        start_char = toks[span.start].start
        a_char, b_char = next(((a, b) for a, b in sentences if a <= start_char < b), (0, len(text)))
        a = next((i for i, t in enumerate(toks) if t.start >= a_char), 0)
        b = next((i for i, t in enumerate(toks) if t.start >= b_char), len(toks))
        leading = [t.norm for t in toks[max(a, span.start - 10):span.start]]
        trailing = [t.norm for t in toks[span.end:min(b, span.end + 7)]]
        trail = _nearest(trailing, _TRAILING, leading=False)
        if trail == MentionType.objection:
            return trail, True
        lead = _nearest(leading, _LEADING, leading=True)
        if lead:
            return lead, True
        if trail:
            return trail, True
        return (MentionType.interest if role == SpeakerRole.customer else MentionType.info_only), False

    @staticmethod
    def _surface(text: str, toks: list[Tok], span: FundSpan, hit: ClassHit | None, extra_ids: list[int]) -> str:
        start = toks[span.start].start
        end = toks[span.end - 1].end
        if hit is not None and not extra_ids and hit.start >= span.end and hit.start - span.end <= 2:
            end = toks[hit.end - 1].end
        return text[start:end].strip(" ,;:.")

    @staticmethod
    def _quote(text: str, toks: list[Tok], span: FundSpan, positives: list[ClassHit], sentences: list[tuple[int, int]]) -> str:
        lo_char, hi_char = toks[span.start].start, toks[span.end - 1].end
        for hit in positives:
            hi_char = max(hi_char, toks[hit.end - 1].end) if hit.start >= span.end else hi_char
        lo = next(((a, b) for a, b in sentences if a <= lo_char < b), (0, len(text)))
        hi = next(((a, b) for a, b in sentences if a < hi_char <= b), lo)
        return text[lo[0]:max(lo[1], hi[1])].strip()

    # ------------------------------------------------------------------ resolution
    def _decide(self, raw: RawProductMention) -> _Decision:
        key = normalize_surface(raw.surface)
        learned = self._learned.get(key)
        if learned is None and self._learned:
            hit = process.extractOne(key, list(self._learned), scorer=fuzz.ratio, score_cutoff=94)
            learned = self._learned[hit[0]] if hit else None
        if learned is not None and raw.share_class_stated and self._products[learned[0]].share_class != raw.share_class_stated:
            learned = None                         # an alias must never override a share class the speaker actually said
        toks = tokenize(raw.surface)
        spans = self.index.find_fund_spans(toks)
        span = max(spans, key=lambda s: len(s.core)) if spans else None
        raw_words = {t.norm for t in toks}
        tokens = span.tokens if span else [t.norm for t in toks]
        fuzzy = bool(span and set(span.tokens) - raw_words)
        matches = self.index.match_families(tokens, limit=5)
        has_brand = bool(span and span.has_brand and not ("contoso" in span.tokens and "woodgrove" in span.tokens))
        if learned is not None:
            fam = self.index.families[self._products[learned[0]].family_id]
            return _Decision([FamilyMatch(fam, 1.0, True)], 0.97, False, True, False, has_brand, forced_product=learned[0],
                             key=fam.family_id)
        if not matches:
            return _Decision([], 0.0, True, raw.branded, fuzzy, has_brand, key="none:" + key)
        top = matches[0]
        second = matches[1] if len(matches) > 1 else None
        confidence = top.score * (1.0 if raw.branded or has_brand else 0.92)
        ambiguous = top.score < 0.72 or (second is not None and top.score - second.score < 0.06)
        key = top.family.family_id if not ambiguous else "amb:" + ",".join(sorted(m.family.family_id for m in matches[:3]))
        return _Decision(matches, confidence, ambiguous, raw.branded, fuzzy, has_brand, key=key)

    def resolve(self, mentions: list[RawProductMention], *, business_line: str, top_k: int = 3,
                review_threshold: float = 0.75) -> list[ProductMention]:
        decided = [(raw, self._decide(raw)) for raw in mentions]
        anchors = {d.matches[0].family.family_id for raw, d in decided if d.matches and not d.ambiguous and d.has_brand}
        anchors |= {d.matches[0].family.family_id for _, d in decided if d.forced_product}
        for _, d in decided:                      # "the global bond fund" -> the fund that was named in full earlier
            if d.matches and not d.has_brand and not d.forced_product and (d.ambiguous or d.matches[0].family.family_id not in anchors):
                option = next((m for m in d.matches[:3] if m.family.family_id in anchors and m.score >= 0.6), None)
                if option is not None:
                    d.matches, d.ambiguous, d.key = [option], False, option.family.family_id
                    d.confidence = max(d.confidence, option.score * 0.92)
        groups: dict[str, _Group] = {}
        for idx, (raw, d) in enumerate(decided):
            g = groups.setdefault(d.key, _Group(d.key, first_segment=min(raw.segment_ids) if raw.segment_ids else idx))
            g.raws.append(raw)
            g.decisions.append(d)
        out: list[ProductMention] = []
        for g in sorted(groups.values(), key=lambda g: g.first_segment):
            if not g.key.startswith("none") and not any(r.branded for r in g.raws) and not any(d.forced_product for d in g.decisions):
                continue                                              # brand-less category talk ("a growth fund"), not a product
            if g.key.startswith("amb:") and not any(d.has_brand for d in g.decisions):
                continue                                              # an unbranded, unresolvable description (e.g. a competitor's fund)
            out.extend(self._resolve_group(g, business_line=business_line, top_k=top_k, review_threshold=review_threshold))
        for n, m in enumerate(out, 1):
            m.mention_id = f"m{n}"
        return out

    @staticmethod
    def _effective_classes(raws: list[RawProductMention]) -> tuple[list[str], set[str]]:
        """Classes the speakers settled on, and the ones they ruled out. A later negation or correction overrides an earlier claim."""
        def seg(r: RawProductMention) -> int:
            return min(r.segment_ids) if r.segment_ids else 0

        positive: dict[str, int] = {}
        negated: dict[str, int] = {}
        for r in raws:
            if r.share_class_stated:
                positive[r.share_class_stated] = max(positive.get(r.share_class_stated, -1), seg(r))
            for c in r.negated_classes:
                negated[c] = max(negated.get(c, -1), seg(r))
        settled = [c for c in positive if not (c in negated and negated[c] >= positive[c])]
        corrections = [(seg(r), r.share_class_stated) for r in raws if r.corrects and r.share_class_stated]
        if corrections:
            at, latest = max(corrections)
            settled = [c for c in settled if c == latest or positive[c] >= at]
        return settled, set(negated)

    def _resolve_group(self, g: _Group, *, business_line: str, top_k: int, review_threshold: float) -> list[ProductMention]:
        d = max(g.decisions, key=lambda x: x.confidence)
        strongest = max(g.raws, key=lambda r: (r.share_class_stated is not None, r.branded, len(r.surface)))
        mtype = self._merge_type(g.raws)
        evidence = self._evidence(g.raws)
        fuzzy = any(x.fuzzy for x in g.decisions)
        if g.key.startswith("none") or not d.matches:
            return [ProductMention(mention_id="", surface=strongest.surface, evidence=evidence, candidates=[], chosen_product_id=None,
                                   confidence=0.0, mention_type=mtype, needs_review=True, reasons=["no_candidate"])]
        if d.ambiguous:
            cands = self._cross_family_candidates(d, strongest.share_class_stated, business_line, top_k)
            return [ProductMention(mention_id="", surface=strongest.surface, evidence=evidence, candidates=cands,
                                   chosen_product_id=None, confidence=round(cands[0].score if cands else 0.0, 3),
                                   mention_type=mtype, share_class_stated=strongest.share_class_stated, needs_review=True,
                                   reasons=["low_margin"] + ([] if strongest.share_class_stated else ["share_class_not_stated"]))]
        fam = d.matches[0].family
        via_alias = d.matches[0].via_alias
        stated, ruled_out = self._effective_classes(g.raws)
        base_on = ["learned_alias"] if d.forced_product else ["alias" if via_alias else "fund_name"]
        if fuzzy:
            base_on.append("fuzzy")
        results: list[ProductMention] = []
        if d.forced_product:
            product = self._products[d.forced_product]
            cands = [self._candidate(product, 0.99, base_on)] + self._others(fam, product.share_class, business_line, top_k - 1, 0.3)
            return [ProductMention(mention_id="", surface=strongest.surface, evidence=evidence, candidates=cands[:top_k],
                                   chosen_product_id=product.product_id, confidence=0.97, mention_type=mtype,
                                   share_class_stated=product.share_class, needs_review=False, reasons=["learned_alias"])]
        if stated:
            for cls in stated:
                raws = [r for r in g.raws if r.share_class_stated == cls]
                results.append(self._with_class(fam, cls, d, raws, base_on, business_line, top_k, review_threshold,
                                                self._merge_type(raws) if len(stated) > 1 else mtype,
                                                self._evidence(raws), len(stated) > 1))
            return results
        if len(fam.products) == 1:
            product = next(iter(fam.products.values()))
            conf = round(min(0.97, 0.60 + 0.37 * d.confidence), 3)
            return [ProductMention(mention_id="", surface=strongest.surface, evidence=evidence,
                                   candidates=[self._candidate(product, conf, base_on)], chosen_product_id=product.product_id,
                                   confidence=conf, mention_type=mtype, needs_review=conf < review_threshold,
                                   reasons=[] if conf >= review_threshold else ["low_margin"])]
        order = [c for c in self.index.class_order(fam, business_line) if c not in ruled_out] or \
            self.index.class_order(fam, business_line)
        weights = [0.5, 0.3, 0.2, 0.1, 0.05]
        cands = [self._candidate(fam.products[c], round(min(0.6, d.confidence * w * 0.9), 3), base_on)
                 for c, w in zip(order, weights, strict=False)][:top_k]
        return [ProductMention(mention_id="", surface=strongest.surface, evidence=evidence, candidates=cands,
                               chosen_product_id=None, confidence=round(0.4 * d.confidence, 3), mention_type=mtype,
                               needs_review=True, reasons=["share_class_not_stated"])]

    def _with_class(self, fam: Family, cls: str, d: _Decision, raws: list[RawProductMention], base_on: list[str],
                    business_line: str, top_k: int, review_threshold: float, mtype: MentionType,
                    evidence: list[Evidence], multi: bool) -> ProductMention:
        surface = max(raws, key=lambda r: len(r.surface)).surface
        if cls not in fam.products:
            order = self.index.class_order(fam, business_line)
            cands = [self._candidate(fam.products[c], round(0.3 / (i + 1), 3), base_on) for i, c in enumerate(order)][:top_k]
            return ProductMention(mention_id="", surface=surface, evidence=evidence, candidates=cands, chosen_product_id=None,
                                  confidence=0.2, mention_type=mtype, share_class_stated=cls, needs_review=True,
                                  reasons=["share_class_unavailable"])
        product = fam.products[cls]
        conf = round(min(0.99, 0.60 + 0.37 * d.confidence), 3)
        cands = [self._candidate(product, conf, [*base_on, "share_class"])] + self._others(fam, cls, business_line, top_k - 1, 0.35)
        return ProductMention(mention_id="", surface=surface, evidence=evidence, candidates=cands[:top_k],
                              chosen_product_id=product.product_id, confidence=conf, mention_type=mtype,
                              share_class_stated=cls, needs_review=conf < review_threshold,
                              reasons=[] if conf >= review_threshold else ["low_margin"])

    # ------------------------------------------------------------------ candidates and evidence
    def _candidate(self, product, score: float, matched_on: list[str]) -> ProductCandidate:
        return ProductCandidate(product_id=product.product_id, display_name=product.display_name, fund_name=product.fund_name,
                                share_class=product.share_class, score=round(score, 3), matched_on=list(matched_on))

    def _others(self, fam: Family, skip_cls: str, business_line: str, n: int, top: float) -> list[ProductCandidate]:
        order = [c for c in self.index.class_order(fam, business_line) if c != skip_cls][:max(n, 0)]
        return [self._candidate(fam.products[c], round(top / (i + 1), 3), ["fund_name"]) for i, c in enumerate(order)]

    def _cross_family_candidates(self, d: _Decision, stated: str | None, business_line: str, top_k: int) -> list[ProductCandidate]:
        out: list[ProductCandidate] = []
        for m in d.matches[:top_k]:
            fam = m.family
            cls = stated if stated in fam.products else self.index.class_order(fam, business_line)[0]
            out.append(self._candidate(fam.products[cls], round(min(0.7, m.score * 0.8), 3),
                                       ["alias" if m.via_alias else "fund_name"] + (["share_class"] if stated in fam.products else [])))
        return out

    @staticmethod
    def _merge_type(raws: list[RawProductMention]) -> MentionType:
        explicit = [r.mention_type for r in raws if r.explicit]
        for typ in _PRIORITY:
            if typ in explicit:
                return typ
        if any(r.mention_type == MentionType.interest for r in raws):
            return MentionType.interest
        return MentionType.info_only

    @staticmethod
    def _evidence(raws: list[RawProductMention]) -> list[Evidence]:
        seen: set[str] = set()
        out: list[Evidence] = []
        for r in raws:
            if r.quote and r.quote not in seen:
                seen.add(r.quote)
                out.append(Evidence(segment_ids=list(r.segment_ids), quote=r.quote))
            if len(out) == 3:
                break
        return out
