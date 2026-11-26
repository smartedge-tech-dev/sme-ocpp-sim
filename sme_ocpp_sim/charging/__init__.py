"""Version-neutral smart charging and power model (plan §5 M4).

``profiles`` holds installed charging profiles, ``composite`` evaluates them into a composite schedule and the
live power limit. Nothing here imports a protocol package; version adapters convert into these domain types.
"""
