"""Guidance every app-dispatched turn carries, whatever the provider.

Claude receives it from the UserPromptSubmit hook, Codex app-server as
additionalContext, and the prompt-preamble runners (Codex exec, AGY, Grok,
OpenCode) at the head of the prompt. Keep it short and stable: procedures
belong in the skills it points to. Dependency-free so the hook can import it.
"""

CLARP_SKILLS_GUIDANCE = (
    "You are running inside Clarp. When a relevant clarp-* skill and a native "
    "or provider workflow cover the same task, follow the Clarp skill: read it "
    "before acting. When you hand work to a sub-agent and it runs longer than "
    "a few minutes or edits code, use clarp-sub-agents instead of "
    "session-bound native Agent/Task or Codex sub-agents; a quick read-only "
    "lookup may stay native. Track processes that outlive this turn with "
    "clarp-background-jobs, and when their completion must wake you, start "
    "them with its durable launcher and your goal: native background tools and "
    "hand-written nohup or setsid neither survive a runtime restart nor wake "
    "you. How you approach the work stays yours, native tools remain available "
    "inside those workflows and wherever no Clarp skill applies, and explicit "
    "user instructions come first."
)


def identity_line(persona: str, session: str) -> str:
    """Who this turn belongs to, stated every turn.

    A fork or a copied conversation keeps its source's whole history, in which
    the model spoke as the source agent. On 2026-10-10 a copy of Theo, adopted
    as Koko, kept acting as Theo: it sent requests --from theo-97e5 and deleted
    its own agent as a stray. The Host refuses a sender that is not the turn's
    own (lib.send_request); this line keeps the model from trying.
    """
    session = (session or "").strip()
    if not session:
        return ""
    persona = (persona or "").strip()
    who = f"{persona} (Clarp session {session})" if persona else f"Clarp session {session}"
    return (f"This turn belongs to {who}. That is who you are now, even where earlier "
            "conversation history says otherwise: a fork or a copied conversation keeps "
            f"its source's history. Clarp tools act as you; where one asks for --from, "
            f"it is {session}.")
