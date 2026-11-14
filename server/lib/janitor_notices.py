"""Durable, app-facing service-authored Janitor notices."""


def conversation_id(agent_id):
    # A read-model namespace, never a fabricated native provider conversation.
    return f"janitor:{agent_id}"


def has_notices(agent_id):
    from . import message_store
    return message_store.has_system_notices(agent_id=agent_id, backend_session_id=conversation_id(agent_id))


def publish(stream, owner, notice_id, text):
    from . import agents, events, message_store
    agent=agents.get_by_agent_id(owner['agent_id'])
    if not agent or not agent.get('is_janitor'):
        raise ValueError('Janitor notices require a Janitor owner')
    native_session=agents.live_backend_session(owner['agent_id'])
    backend_session=conversation_id(owner['agent_id'])
    row = message_store.record_system_notice(agent_id=owner['agent_id'],
        backend_session_id=backend_session, notice_id=notice_id, text=text)
    if row is None:
        raise ValueError('service notice not persisted')
    if stream is not None:
        # Retrying a lost acknowledgement may repeat this revision-watermarked
        # refresh event, but never the message or a push notification.
        events.broadcast(stream, events.transcript_updated(agent_id=owner['agent_id'],
            session=owner['session'], backend_session_id=native_session or backend_session))
    return row
