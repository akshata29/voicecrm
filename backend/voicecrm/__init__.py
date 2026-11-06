"""VoiceCRM - conversation-to-CRM hub (proof of concept).

Layering: domain (pydantic models) -> ports (Protocols) -> engines (pure logic) -> adapters
(mock/local + live Azure implementations) -> pipeline (orchestration) -> api (FastAPI).
"""

__version__ = "0.1.0"
