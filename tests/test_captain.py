"""Captain sessions started by a BLiP icebreaker (CAPTAIN_* settings)."""

from unittest.mock import AsyncMock, call

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient
from app.integrations.errors import IntegrationError
from app.models import ConversationMapping, InboundEvent, MessageDelivery
from app.services.bridge import BridgeService
from app.services.whatsapp_format import markdown_to_whatsapp

CUSTOMER = "551199999999@wa.gw.msging.net"
SUBBOT = "puccampinascamila@msging.net"


@pytest.fixture
def captain_settings(settings: Settings) -> Settings:
    settings.captain_trigger_phrases = "Resgatar meu cupom."
    settings.captain_session_labels = "camila, bot-venda-inicio"
    settings.captain_blip_subbot = SUBBOT
    settings.captain_replies_to_blip = True
    return settings


def _chatwoot(*, conversation_status: str = "pending", labels: list[str] | None = None):
    chatwoot = AsyncMock(spec=ChatwootClient)
    chatwoot.create_contact.return_value = {
        "payload": {
            "contact": {"id": 100},
            "contact_inbox": {"source_id": "source-1", "inbox": {"id": 20}},
        }
    }
    chatwoot.create_conversation.return_value = {"id": 200, "status": conversation_status}
    chatwoot.create_message.return_value = {"id": 300}
    chatwoot.get_messages.return_value = []
    chatwoot.get_conversation_labels.return_value = list(labels or [])
    return chatwoot


def _customer_event(content: str = "Resgatar meu cupom.", message_id: str = "m-1") -> InboundEvent:
    return InboundEvent(
        provider="blip",
        external_id=f"message:{message_id}",
        event_type="message",
        payload={
            "id": message_id,
            "from": CUSTOMER,
            "to": "mybot@msging.net",
            "type": "text/plain",
            "content": content,
        },
    )


def _mapping(settings: Settings, **overrides) -> ConversationMapping:
    values = {
        "blip_bot_identity": settings.blip_bot_identity,
        "blip_customer_identity": CUSTOMER,
        "chatwoot_account_id": settings.chatwoot_account_id,
        "chatwoot_contact_id": 100,
        "chatwoot_source_id": "source-1",
        "chatwoot_conversation_id": 200,
    }
    values.update(overrides)
    return ConversationMapping(**values)


async def _process(session, settings, blip, chatwoot, event) -> None:
    session.add(event)
    await session.commit()
    service = BridgeService(session=session, settings=settings, blip=blip, chatwoot=chatwoot)
    if event.provider == "blip":
        await service.process_blip_message(event)
    else:
        await service.process_chatwoot_event(event)


# --- settings ------------------------------------------------------------------------------


def test_captain_is_off_by_default(settings) -> None:
    assert not settings.captain_enabled
    assert not settings.blip_writes_enabled


def test_captain_phrases_and_labels_are_parsed(captain_settings) -> None:
    captain_settings.captain_trigger_phrases = " Resgatar  meu CUPOM. | Quero meu desconto "
    assert captain_settings.captain_trigger_phrase_set == {
        "resgatar meu cupom.",
        "quero meu desconto",
    }
    assert captain_settings.captain_session_label_list == ["camila", "bot-venda-inicio"]
    assert captain_settings.blip_writes_enabled


def test_captain_phrases_require_the_subbot() -> None:
    with pytest.raises(ValidationError, match="CAPTAIN_BLIP_SUBBOT"):
        Settings(_env_file=None, captain_trigger_phrases="Resgatar meu cupom.")


def test_follow_master_state_enables_the_captain_and_needs_the_subbot() -> None:
    with pytest.raises(ValidationError, match="CAPTAIN_BLIP_SUBBOT"):
        Settings(_env_file=None, captain_follow_master_state=True)
    settings = Settings(
        _env_file=None, captain_follow_master_state=True, captain_blip_subbot=SUBBOT
    )
    assert settings.captain_enabled
    assert settings.blip_writes_enabled
    assert not settings.captain_trigger_phrase_set


# --- icebreaker ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_icebreaker_starts_the_session_before_the_message_is_posted(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        event = _customer_event("  resgatar   MEU cupom. ")
        await _process(session, captain_settings, blip, chatwoot, event)

        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is True
        assert mapping.status == "pending"
        assert mapping.chatwoot_labels == ["camila", "bot-venda-inicio"]
        # Created pending (Captain on the inbox) and left pending: never opened, never toggled.
        chatwoot.toggle_status.assert_not_awaited()
        names = [name for name, *_ in chatwoot.mock_calls]
        assert names.index("add_conversation_labels") < names.index("create_message")
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["camila", "bot-venda-inicio"]
        )
        assert event.status == "processed"
        blip.get_master_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_icebreaker_in_an_open_conversation_moves_it_to_pending(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot(labels=["vip", "camila"])
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="open"))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event())

        mapping = await session.scalar(select(ConversationMapping))
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["vip", "camila", "bot-venda-inicio"]
        )
        chatwoot.ensure_labels.assert_awaited_once_with(["bot-venda-inicio"])
        assert mapping.captain_active is True
        assert mapping.status == "pending"


@pytest.mark.asyncio
async def test_new_conversation_created_open_goes_pending_on_icebreaker(
    session_factory, captain_settings
) -> None:
    chatwoot = _chatwoot(conversation_status="open")
    async with session_factory() as session:
        await _process(
            session, captain_settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event()
        )
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")


@pytest.mark.asyncio
async def test_retry_after_the_message_was_posted_does_not_restart_the_session(
    session_factory, captain_settings
) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="open"))
        event = _customer_event()
        event.result_id = "300"
        await _process(session, captain_settings, AsyncMock(spec=BlipClient), chatwoot, event)

        chatwoot.add_conversation_labels.assert_not_awaited()
        chatwoot.create_message.assert_not_awaited()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["Resgatar meu cupom agora", "oi"])
async def test_other_text_is_not_an_icebreaker(session_factory, captain_settings, content) -> None:
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(
            session,
            captain_settings,
            AsyncMock(spec=BlipClient),
            chatwoot,
            _customer_event(content),
        )
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="open")
        chatwoot.add_conversation_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_icebreaker_is_ignored_when_the_feature_is_off(session_factory, settings) -> None:
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, settings, AsyncMock(spec=BlipClient), chatwoot, _customer_event())
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False
        assert mapping.status == "open"
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="open")


@pytest.mark.asyncio
async def test_bot_message_with_the_phrase_is_not_an_icebreaker(
    session_factory, captain_settings
) -> None:
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="open"))
        event = _customer_event()
        event.payload = {
            **event.payload,
            "from": "mybot@msging.net/instance",
            "to": CUSTOMER,
        }
        await _process(session, captain_settings, AsyncMock(spec=BlipClient), chatwoot, event)
        chatwoot.add_conversation_labels.assert_not_awaited()


# --- customer returns ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("master_state", "status", "active"),
    [
        (SUBBOT, "pending", True),
        ("PucCampinasCamila@msging.net/instance-1", "pending", True),
        (None, "open", False),
        ("puccampinascomercial@msging.net", "open", False),
    ],
)
async def test_returning_customer_follows_the_router_state(
    session_factory, captain_settings, master_state, status, active
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = master_state
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="resolved", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))

        mapping = await session.scalar(select(ConversationMapping))
        blip.get_master_state.assert_awaited_once_with(CUSTOMER)
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status=status)
        assert mapping.status == status
        assert mapping.captain_active is active
        names = [name for name, *_ in chatwoot.mock_calls]
        assert names.index("toggle_status") < names.index("create_message")


@pytest.mark.asyncio
async def test_pending_session_with_the_customer_still_there_is_left_alone(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="pending", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("quero IA"))
        chatwoot.toggle_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_router_state_keeps_the_captain(session_factory, captain_settings) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.side_effect = IntegrationError("down", retryable=True, status_code=503)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="resolved", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")


@pytest.mark.asyncio
async def test_open_session_conversation_is_not_touched(session_factory, captain_settings) -> None:
    """After a handoff the conversation is open: the customer's messages change nothing."""
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="open", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))
        blip.get_master_state.assert_not_awaited()
        chatwoot.toggle_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_without_session_reopens_as_before(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="resolved"))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))
        blip.get_master_state.assert_not_awaited()
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="open")


# --- BLiP decides (night window): CAPTAIN_FOLLOW_MASTER_STATE ----------------------------------


@pytest.fixture
def follow_settings(captain_settings: Settings) -> Settings:
    captain_settings.captain_follow_master_state = True
    return captain_settings


@pytest.mark.asyncio
async def test_any_text_starts_the_session_while_the_router_has_the_customer_in_the_subbot(
    session_factory, follow_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot(conversation_status="open")
    async with session_factory() as session:
        await _process(session, follow_settings, blip, chatwoot, _customer_event("boa noite"))

        mapping = await session.scalar(select(ConversationMapping))
        blip.get_master_state.assert_awaited_once_with(CUSTOMER)
        assert mapping.captain_active is True
        assert mapping.status == "pending"
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")
        chatwoot.add_conversation_labels.assert_awaited_once_with(
            conversation_id=200, labels=["camila", "bot-venda-inicio"]
        )
        names = [name for name, *_ in chatwoot.mock_calls]
        assert names.index("toggle_status") < names.index("create_message")


@pytest.mark.asyncio
async def test_new_conversation_of_a_customer_in_the_subbot_is_not_opened(
    session_factory, follow_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, follow_settings, blip, chatwoot, _customer_event("boa noite"))
        # created pending by Chatwoot and kept pending: the only status change is none
        chatwoot.toggle_status.assert_not_awaited()
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.status == "pending"
        assert mapping.captain_active is True


@pytest.mark.asyncio
async def test_resolved_conversation_of_a_customer_back_in_the_subbot_goes_pending(
    session_factory, follow_settings
) -> None:
    """Next night: the Captain resolved the last conversation and BLiP sends the customer again."""
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = SUBBOT
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(follow_settings, status="resolved"))
        await session.commit()
        await _process(session, follow_settings, blip, chatwoot, _customer_event("oi"))

        mapping = await session.scalar(select(ConversationMapping))
        chatwoot.toggle_status.assert_awaited_once_with(conversation_id=200, status="pending")
        assert mapping.captain_active is True


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "puccampinascomercial@msging.net"])
async def test_customer_outside_the_subbot_is_handled_as_before(
    session_factory, follow_settings, state
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.return_value = state
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, follow_settings, blip, chatwoot, _customer_event("oi"))
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False
        assert mapping.status == "open"
        chatwoot.add_conversation_labels.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_state_starts_no_session_and_still_mirrors(
    session_factory, follow_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    blip.get_master_state.side_effect = IntegrationError("down", retryable=True, status_code=503)
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, follow_settings, blip, chatwoot, _customer_event("oi"))
        mapping = await session.scalar(select(ConversationMapping))
        assert mapping.captain_active is False
        chatwoot.create_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_state_read_when_a_session_is_active_or_the_message_was_already_posted(
    session_factory, follow_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(follow_settings, status="open", captain_active=True))
        await session.commit()
        await _process(session, follow_settings, blip, chatwoot, _customer_event("oi", "m-1"))
        retry = _customer_event("oi", "m-2")
        retry.result_id = "300"
        await _process(session, follow_settings, blip, chatwoot, retry)
        blip.get_master_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_messages_never_trigger_the_state_read(session_factory, follow_settings) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(follow_settings, status="open"))
        event = _customer_event("Olá!")
        event.payload = {**event.payload, "from": "mybot@msging.net/instance", "to": CUSTOMER}
        await _process(session, follow_settings, blip, chatwoot, event)
        blip.get_master_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_state_is_not_read_when_the_switch_is_off(session_factory, captain_settings) -> None:
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot(conversation_status="pending")
    async with session_factory() as session:
        await _process(session, captain_settings, blip, chatwoot, _customer_event("oi"))
        blip.get_master_state.assert_not_awaited()


# --- Captain replies -----------------------------------------------------------------------


def _chatwoot_message(*, sender: str = "captain_assistant", **overrides) -> InboundEvent:
    payload = {
        "event": "message_created",
        "id": 400,
        "content": "Plano 1: **12x de R$ 635,17**",
        "message_type": "outgoing",
        "private": False,
        "sender": {"type": sender},
        "conversation": {"id": 200},
    }
    payload.update(overrides)
    return InboundEvent(
        provider="chatwoot",
        external_id="message_created:400",
        event_type="message_created",
        payload=payload,
    )


@pytest.mark.asyncio
async def test_captain_reply_in_a_session_is_sent_to_blip_as_whatsapp_text(
    session_factory, captain_settings
) -> None:
    captain_settings.chatwoot_replies_allowed_conversations = "999"  # agents' pilot list
    blip = AsyncMock(spec=BlipClient)
    chatwoot = _chatwoot()
    async with session_factory() as session:
        session.add(_mapping(captain_settings, status="pending", captain_active=True))
        await session.commit()
        await _process(session, captain_settings, blip, chatwoot, _chatwoot_message())

        blip.send_message.assert_awaited_once()
        sent = blip.send_message.await_args.kwargs
        assert sent["to"] == CUSTOMER
        assert sent["message_type"] == "text/plain"
        assert sent["content"] == "Plano 1: *12x de R$ 635,17*"
        delivery = await session.scalar(select(MessageDelivery))
        assert delivery.status == "sent"
        assert chatwoot.update_message_status.await_args == call(
            conversation_id=200, message_id=400, status="sent"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "switch", "overrides"),
    [
        (False, True, {}),
        (True, False, {}),
        (True, True, {"private": True}),
        (True, True, {"content_attributes": {"blip_message_id": "echo"}}),
    ],
)
async def test_captain_reply_is_not_forwarded_outside_a_session(
    session_factory, captain_settings, active, switch, overrides
) -> None:
    captain_settings.captain_replies_to_blip = switch
    blip = AsyncMock(spec=BlipClient)
    async with session_factory() as session:
        session.add(_mapping(captain_settings, captain_active=active))
        await session.commit()
        await _process(
            session, captain_settings, blip, _chatwoot(), _chatwoot_message(**overrides)
        )
        blip.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_captain_switch_does_not_forward_human_agents(
    session_factory, captain_settings
) -> None:
    blip = AsyncMock(spec=BlipClient)
    async with session_factory() as session:
        session.add(_mapping(captain_settings, captain_active=True))
        await session.commit()
        await _process(
            session, captain_settings, blip, _chatwoot(), _chatwoot_message(sender="user")
        )
        blip.send_message.assert_not_awaited()


# --- BLiP master-state ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (200, {"status": "success", "resource": SUBBOT}, SUBBOT),
        (200, {"status": "failure", "reason": {"code": 67, "description": "not found"}}, None),
        (404, {"status": "failure"}, None),
    ],
)
async def test_master_state_lookup(settings, status_code, body, expected) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    blip = BlipClient(settings, client)
    assert await blip.get_master_state(CUSTOMER) == expected
    await client.aclose()

    sent = requests[0]
    assert sent.url.path.endswith("/commands")
    assert sent.headers["Authorization"] == "Key blip-key"
    payload = __import__("json").loads(sent.content)
    assert payload["to"] == "postmaster@msging.net"
    assert payload["method"] == "get"
    assert payload["uri"] == f"/contexts/{CUSTOMER}/master-state"


@pytest.mark.asyncio
async def test_master_state_other_failure_raises(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "failure", "reason": {"code": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(IntegrationError):
        await BlipClient(settings, client).get_master_state(CUSTOMER)
    await client.aclose()


# --- formatting ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("**IA e Negócios** em **16/11/2026**", "*IA e Negócios* em *16/11/2026*"),
        ("__sublinhado__ e ~~riscado~~", "*sublinhado* e ~riscado~"),
        ("## Planos\n* Plano 1\n* Plano 2", "*Planos*\n- Plano 1\n- Plano 2"),
        ("[Pague aqui](https://x.example/p?a=1)", "Pague aqui: https://x.example/p?a=1"),
        ("[https://x.example](https://x.example)", "https://x.example"),
        ("2**3 e *já negrito*", "2**3 e *já negrito*"),
        ("sem formatação", "sem formatação"),
    ],
)
def test_markdown_to_whatsapp(text, expected) -> None:
    assert markdown_to_whatsapp(text) == expected
