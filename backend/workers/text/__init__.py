"""Text-agent LiveKit worker package.

Process entry: ``python -m workers.text.main``

Layout (one concern per module):

- ``main`` / ``actor`` — transport and per-conversation concurrency
- ``executor`` — load → prepare → run → park
- ``*_executor`` — plane-specific cold start
- ``turn`` / ``steps`` / ``fanout`` / ``window`` — turn lifecycle pieces
- ``planes`` / ``types`` / ``events`` / ``load`` / ``interruption`` — shared pieces
"""
