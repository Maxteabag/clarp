"""Transcription vocabulary: what the speech model is primed with.

Moved out of ServerContext so the context holds services, not logic. A
VocabService needs only the current STT (for its provider and model); it is
built per call because the STT can be swapped at runtime.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .log import log_exception
from .vocab import delegation_agent_names_enabled, read_technical_glossary


@dataclass(frozen=True)
class TranscriptionVocab:
    """What /transcribe sends the model, and the audit row it belongs to."""
    payload: str
    run_id: int
    provider: str
    model: str


class VocabService:
    def __init__(self, stt: Callable[[], object]):
        self._stt = stt

    def active_agent_names(self) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()

        def add(name: object) -> None:
            text = str(name or "").strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                names.append(text)

        try:
            from . import agents as agents_db  # local: avoid startup cycles
            for info in agents_db.list_agents():
                if agents_db.interaction_capabilities(info)["can_voice_target"]:
                    add(info.get("persona"))
        except Exception as e:  # noqa: BLE001 - prompt bias must never break STT
            log_exception("vocabAgentsFail", e)
        return names

    def prompt(self, *, delegated: bool) -> str:
        """Legacy string form of the biasing prompt.

        Retained for callers that only want a prompt. Prefer
        `vocab_compile_result` when the caller can record the audit row -
        that is the path that makes a transcript traceable to its prompt.
        """
        return self.compile_result(delegated=delegated).payload

    def for_transcription(
        self,
        *,
        delegated: bool,
        session: str = "",
        trace_id: str = "",
        requested_model: str = "",
    ) -> TranscriptionVocab:
        """Everything a real /transcribe needs from the vocabulary system.

        Resolves the focused agent (its profile, its workspace), the provider
        and model actually answering, the recent speech and known corrections,
        compiles, and writes the audit row. Returns the payload plus the run id
        so the handler can attach the transcript and latency afterwards.
        """
        from . import agents as agents_db
        from . import vocab_store
        from .vocab_compile import Sources, compile_and_record
        from .workspace_vocab import WorkspaceSources, sources_for

        provider, model = self._provider_model(requested_model)
        agent: dict = {}
        if session:
            try:
                agent = agents_db.get_by_session(session) or {}
            except Exception as e:  # noqa: BLE001
                log_exception("vocabAgentLookupFail", e)
        agent_id = str(agent.get("agent_id") or "")

        static, profile_id = self._static_packs(
            delegated=delegated, agent_id=agent_id)
        try:
            recent = vocab_store.recent_transcripts(session) if session else ()
        except Exception as e:  # noqa: BLE001
            log_exception("vocabRecentFail", e)
            recent = ()
        try:
            known = vocab_store.corrections()
        except Exception as e:  # noqa: BLE001
            log_exception("vocabCorrectionsFail", e)
            known = ()
        # Reading an agent's folders is a choice its profile makes. Without
        # one, the workspace contributes nothing: this call used to be
        # unconditional, which is how an agent rooted at /home fed hundreds of
        # package-tree library names into the payload ahead of the glossary.
        workspace = (
            sources_for(agent.get("cwd"))
            if vocab_store.agent_harvests_workspace(agent_id)
            else WorkspaceSources())
        include_names = delegated and delegation_agent_names_enabled()

        result = compile_and_record(
            provider=provider, model=model, static_packs=static,
            sources=Sources(
                agent_names=(
                    tuple(self.active_agent_names()) if include_names else ()),
                project_name=workspace.project_name,
                identifiers=workspace.identifiers,
                branches=workspace.branches,
                commit_subjects=workspace.commit_subjects,
                recent_transcripts=recent,
                corrections=known,
            ),
            agent_id=agent_id, session=session, trace_id=trace_id,
            profile_id=profile_id,
        )
        return TranscriptionVocab(
            payload=result.payload, run_id=result.run_id,
            provider=provider, model=model)

    def preview(self, *, session: str = "", requested_model: str = "",
                      delegated: bool = False) -> dict:
        """What the next compile would send, per pack, without recording it.

        The live budget meter in the app calls this as the user toggles packs,
        so it must reflect exactly the inputs a real turn would use.
        """
        from . import agents as agents_db
        from . import vocab_store
        from .vocab_compile import Sources, compile_for
        from .workspace_vocab import WorkspaceSources, sources_for

        provider, model = self._provider_model(requested_model)
        agent: dict = {}
        if session:
            try:
                agent = agents_db.get_by_session(session) or {}
            except Exception as e:  # noqa: BLE001
                log_exception("vocabAgentLookupFail", e)
        agent_id = str(agent.get("agent_id") or "")
        static, profile_id = self._static_packs(
            delegated=delegated, agent_id=agent_id)
        workspace = (
            sources_for(agent.get("cwd"))
            if vocab_store.agent_harvests_workspace(agent_id)
            else WorkspaceSources())
        include_names = delegated and delegation_agent_names_enabled()
        result = compile_for(
            provider=provider, model=model, static_packs=static,
            sources=Sources(
                agent_names=(
                    tuple(self.active_agent_names()) if include_names else ()),
                project_name=workspace.project_name,
                identifiers=workspace.identifiers,
                branches=workspace.branches,
                commit_subjects=workspace.commit_subjects,
                recent_transcripts=(
                    vocab_store.recent_transcripts(session) if session else ()),
                corrections=vocab_store.corrections(),
            ))
        audit = result.audit()
        per_pack: dict[str, dict] = {}
        for term in audit["included"]:
            slot = per_pack.setdefault(term["pack"], {"included": 0, "dropped": 0})
            slot["included"] += 1
        for term in audit["dropped"]:
            slot = per_pack.setdefault(term["pack"], {"included": 0, "dropped": 0})
            slot["dropped"] += 1
        return {
            "provider": provider, "model": model, "session": session,
            "agent_id": agent_id, "profile_id": profile_id,
            "unit": audit["unit"], "capacity": audit["capacity"],
            "used": audit["used"], "headroom": audit["headroom"],
            "form": audit["form"], "rarity_floor": audit["rarity_floor"],
            "payload": audit["payload"],
            "included": audit["included"], "dropped": audit["dropped"],
            "packs": [{"pack": name, **counts} for name, counts in per_pack.items()],
        }

    def _provider_model(self, requested_model: str
                                      ) -> tuple[str, str]:
        selected = (requested_model or "").strip()
        if selected and selected != "server-default":
            if ":" in selected:
                provider, model = selected.split(":", 1)
                return provider, model
            return selected, ""
        stt = self._stt()
        provider = str(getattr(stt, "provider", "") or "faster-whisper")
        model = str(getattr(stt, "model_name", "") or "")
        return provider, model

    def _static_packs(self, *, delegated: bool, agent_id: str):
        """Glossary setting plus the agent's assigned profile, if any."""
        from . import vocab_store
        from .vocab_budget import Pack, Term
        from .vocab_generators import estimate_rarity

        static: list[Pack] = []
        profile_id: str | None = None
        if not delegated:
            glossary = read_technical_glossary()
            terms = tuple(
                Term(text=line, pack="glossary", rarity=estimate_rarity(line))
                for line in (raw.strip() for raw in glossary.splitlines())
                if line
            )
            if terms:
                static.append(Pack(
                    name="glossary", terms=terms, priority=1.5, floor=3))
        if agent_id:
            try:
                profile_id = vocab_store.profile_for_agent(agent_id)
                if profile_id:
                    static.extend(vocab_store.profile_packs(profile_id))
            except Exception as e:  # noqa: BLE001
                log_exception("vocabProfileFail", e)
        return static, profile_id

    def compile_result(
        self,
        *,
        delegated: bool,
        provider: str = "faster-whisper",
        model: str = "",
        recent_transcripts: tuple[str, ...] = (),
        corrections: tuple[tuple[str, str], ...] = (),
    ):
        """Compile this turn's biasing payload against the active model.

        The two long-standing settings still govern what may contribute:
        delegation turns are primed with agent names, ordinary turns with the
        user's technical glossary. Everything below that is new - the glossary
        becomes a static pack and the rest is generated, then all of it is
        fitted to whatever the provider actually accepts.
        """
        from .vocab_compile import Sources, compile_for

        include_names = delegated and delegation_agent_names_enabled()
        # The glossary is curated by hand, so it earns a floor: a term the
        # user typed should not be crowded out by generated workspace noise.
        static, _profile = self._static_packs(delegated=delegated, agent_id="")

        return compile_for(
            provider=provider,
            model=model,
            static_packs=static,
            sources=Sources(
                agent_names=(
                    tuple(self.active_agent_names()) if include_names else ()),
                recent_transcripts=recent_transcripts,
                corrections=corrections,
            ),
        )
