"""Cost model: turns recorded usage into an (illustrative) per-call cost using the pricing table.

Lines mirror the billable meters - Speech (audio hours), the LLM (tokens in/out per model), AI Language PII (records) and
Search (queries). Usage recorded by the rules engine as provider "projected" is what the LLM engine *would* have used:
it is shown as its own line but excluded from the total, so the figure answers both "what did this call cost" and
"what would the model-based summary add".
"""
from __future__ import annotations

from collections import defaultdict

from ..domain.metering import CostBreakdown, CostLine, LlmPrice, PricingTable, UsageRecord


class PricingCostEngine:
    """Implements engines.contracts.CostEngine."""

    def estimate(self, usage: list[UsageRecord], pricing: PricingTable) -> CostBreakdown:
        speech_seconds = 0.0
        pii_records = 0.0
        search_queries = 0.0
        llm_in: dict[str, int] = defaultdict(int)
        llm_out: dict[str, int] = defaultdict(int)
        projected_in = projected_out = 0
        for u in usage:
            if u.service == "speech":
                speech_seconds += u.audio_seconds or 0.0
            elif u.service == "language_pii":
                pii_records += u.units or 0.0
            elif u.service == "search":
                search_queries += u.units or 0.0
            elif u.service == "llm":
                if u.provider == "projected":
                    projected_in += u.tokens_in or 0
                    projected_out += u.tokens_out or 0
                else:
                    model = u.model or "default"
                    llm_in[model] += u.tokens_in or 0
                    llm_out[model] += u.tokens_out or 0
        lines: list[CostLine] = []
        if speech_seconds:
            hours = speech_seconds / 3600.0
            lines.append(CostLine(service="speech", quantity=round(hours, 4), unit="audio hours",
                                  est_cost=round(hours * pricing.speech_per_audio_hour, 6)))
        for model in sorted({*llm_in, *llm_out}):
            price = self._price(pricing, model)
            cost = llm_in[model] / 1e6 * price.input_per_1m_tokens + llm_out[model] / 1e6 * price.output_per_1m_tokens
            lines.append(CostLine(service=f"llm:{model}", quantity=float(llm_in[model] + llm_out[model]), unit="tokens (in+out)",
                                  est_cost=round(cost, 6)))
        if pii_records:
            lines.append(CostLine(service="language_pii", quantity=pii_records, unit="text records",
                                  est_cost=round(pii_records / 1000.0 * pricing.language_pii_per_1k_records, 6)))
        if search_queries:
            lines.append(CostLine(service="search", quantity=search_queries, unit="queries",
                                  est_cost=round(search_queries / 1000.0 * pricing.search_per_1k_queries, 6)))
        total = round(sum(line.est_cost for line in lines), 6)
        if projected_in or projected_out:
            price = self._price(pricing, "default")
            cost = projected_in / 1e6 * price.input_per_1m_tokens + projected_out / 1e6 * price.output_per_1m_tokens
            lines.append(CostLine(service="llm_projected (not billed - rules engine)", quantity=float(projected_in + projected_out),
                                  unit="tokens (in+out)", est_cost=round(cost, 6)))
        return CostBreakdown(lines=lines, total=total, currency=pricing.currency, illustrative=pricing.illustrative)

    @staticmethod
    def _price(pricing: PricingTable, model: str) -> LlmPrice:
        return pricing.llm.get(model) or pricing.llm.get("default") or LlmPrice(input_per_1m_tokens=0.0, output_per_1m_tokens=0.0)
