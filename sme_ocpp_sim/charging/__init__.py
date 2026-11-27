"""Version-neutral EV and power model (plan §5 M4).

``ev`` holds the EV models, ``power`` steps the per-connector power, energy and SoC. Nothing here imports a
protocol package; the charging-profile rules are per version (``protocol.v16.profiles`` / ``composite``).
"""
