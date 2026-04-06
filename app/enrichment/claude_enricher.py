"""
Claude (Anthropic) description enricher.

Uses the Claude API as a high-quality fallback to generate instrument descriptions
when yfinance and FMP return nothing.

Set ANTHROPIC_API_KEY in .env to enable. If not set, this enricher is silently skipped.

Cost note: descriptions are cached permanently (stored in the instruments table) so each
instrument is only ever queried once unless last_enriched_at is manually reset.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = (
    'In 100 words give an overview of "{name}" for a share details application. '
    "Be factual and concise. Do not include phrases like 'as of my knowledge cutoff' "
    "or similar caveats."
)


class ClaudeDescriptionEnricher:
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5"):
        self.api_key = api_key
        self.model = model
        self._client = None  # lazy-init so import errors surface at call time

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy import — only required if key is configured
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def get_description(self, instrument_name: str) -> Optional[str]:
        """Ask Claude for a ~100-word description of the named instrument.

        Args:
            instrument_name: Human-readable name, e.g. 'AEW UK REIT' or 'Vanguard FTSE All-World ETF'

        Returns:
            Description string, or None if the call fails.
        """
        prompt = _PROMPT_TEMPLATE.format(name=instrument_name)
        try:
            client = self._get_client()
            message = client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text.strip() if message.content else None
            if text:
                logger.info("  [claude] description fetched for '%s'", instrument_name)
            return text or None
        except Exception as exc:
            logger.warning("  [claude] description failed for '%s': %s", instrument_name, exc)
            return None
