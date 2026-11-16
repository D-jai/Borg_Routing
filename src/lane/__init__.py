"""The on-premise lane: raw document text goes only to models on this machine.

Design: ``docs/design/on_premise_lane.md``. Modules:

- ``errors``     LaneError and the BRT-LANE-* codes
- ``settings``   lane_mode and the lane's limits (config key, then env)
- ``documents``  documents by reference under BORG_DOCS_ROOT
- ``backstop``   the content check on every request headed off the machine
- ``context``    the run's lane state, raw marking helpers, the output guard
- ``outbound``   send_outbound(): call guard, backstop and the ledger

Locality (what counts as "this machine") is ``src/registry/locality.py``.
"""
