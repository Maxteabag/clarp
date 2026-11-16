# Main 029a8601 context builder: byte-compatibility baseline before R21.
def _build_additional_context(*, app_dispatched: bool, voiced: bool,
                              agent: dict | None = None) -> str:
    """Compose the UserPromptSubmit additionalContext for this turn.

    The Clarp-skills guidance is included for every app-dispatched turn; the <speak> voice guidance is appended only on spoken turns.
    Returns "" when neither applies (e.g. a third-party local terminal)."""
    parts = []
    guidance = _clarp_guidance() if app_dispatched else ""
    if guidance:
        parts.append(guidance)
    identity = _identity(agent) if app_dispatched else ""
    if identity:
        parts.append(identity)
    if voiced:
        parts.append(_SPEAK_INSTRUCTIONS + _natural_delivery(agent))
    header = _group_call_header(agent) if app_dispatched else ""
    if header:
        parts.append(header)
    return "\n\n".join(parts)

