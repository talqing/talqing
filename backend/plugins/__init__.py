"""LiveKit plugins Talqing implements because the provider ships none.

Everything here is a plugin in LiveKit's sense — an `stt.STT` / `tts.TTS` /
`llm.LLM` the compiler can hand to an `AgentSession` — and nothing here is
Talqing-specific beyond that. A package lands in this tree only when the vendor
has no plugin of their own to install: the moment one is published, the entry in
`compiler/factories.py` switches to it and the module here is deleted.

That is deliberately a narrow bar. Small adapters *around* a vendor's plugin
(renaming the model it reports, bridging a missing end-of-speech event) stay
beside the factory that builds them in `compiler/factories.py`; this tree is for
whole protocol implementations only.
"""
