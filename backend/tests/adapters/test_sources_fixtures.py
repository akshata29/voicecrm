"""Shared in-memory ScriptedCall builders for the source adapter tests (synthetic data only; no tests live here)."""
from __future__ import annotations

from voicecrm.domain.conversation import Participant
from voicecrm.domain.enums import Channel, Direction, SpeakerRole
from voicecrm.domain.fixtures import GoldLabels, ScriptedCall, ScriptedTurn

SELLER = Participant(id="seller", display_name="Rodney Seller", email="rodney.seller@contoso.example",
                     role=SpeakerRole.seller)
PRIYA = Participant(id="cust1", display_name="Priya Raman", email="priya.raman@meridianpension.example",
                    role=SpeakerRole.customer, org="Meridian Pension Fund", phone="+1-555-0100")
MARCUS = Participant(id="cust2", display_name="Marcus Lee", email="marcus.lee@meridianpension.example",
                     role=SpeakerRole.customer)
DANA = Participant(id="int1", display_name="Dana Colleague", role=SpeakerRole.internal)


def make_call(
    fixture_id: str = "pb-teams-equity-class-c", *, channel: Channel = Channel.teams, language: str = "en-US",
    turns: list[ScriptedTurn] | None = None, direction: Direction = Direction.outbound, duration_minutes: int = 12,
) -> ScriptedCall:
    return ScriptedCall(
        fixture_id=fixture_id, title="Meridian equity review", description="synthetic test call", channel=channel,
        direction=direction, language=language, duration_minutes=duration_minutes, seller=SELLER,
        customers=[PRIYA, MARCUS], internal=[DANA], customer_org="Meridian Pension Fund",
        call_type="product_recommendation",
        turns=turns or [
            ScriptedTurn(speaker="seller", text="Hello Priya, thanks for joining. Let's talk about Contoso Equity Fund, class C."),
            ScriptedTurn(speaker="customer", text="Great. I care about fees & expenses <a lot>."),
            ScriptedTurn(speaker="customer", speaker_id="cust2", text="Same here - what is the expense ratio?"),
            ScriptedTurn(speaker="internal", text="I can send the fee schedule by Friday."),
            ScriptedTurn(speaker="seller", text="Perfect: I'll follow up with you on Monday."),
        ],
        gold=GoldLabels(template_id="pb_institutional"),
    )


def spanish_call(fixture_id: str = "pb-zoom-es-renta-fija") -> ScriptedCall:
    return make_call(
        fixture_id, channel=Channel.zoom, language="es-US",
        turns=[
            ScriptedTurn(speaker="seller", locale="es-US",
                         text="Hola Priya, gracias por conectarse. Quería hablar con usted sobre el fondo de renta fija."),
            ScriptedTurn(speaker="customer", locale="es-US",
                         text="Buenos días, Rodney. Me interesa la clase A, pero también las comisiones de la clase C."),
            ScriptedTurn(speaker="seller", locale="es-US",
                         text="Perfecto, le enviaré la información del fondo y los gastos antes del viernes."),
            ScriptedTurn(speaker="customer", locale="es-US",
                         text="Muy bien, quedamos así. Gracias por su tiempo y hasta la próxima semana."),
        ],
    )
