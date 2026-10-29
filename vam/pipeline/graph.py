"""Stage graph per job kind. A stage becomes runnable when every dependency succeeded; a stage
whose dependency failed or was skipped is skipped, except join stages (publish) which run once
all their dependencies are terminal, whatever the outcome (speech/visual branches are independent).

`prepare` only validates the source and prepares playback (a bad source fails both branches);
audio extraction belongs to the speech branch so an audio problem cannot stop the visual branch.
A job whose graph has no join ends when all its stages are terminal (see repository._advance).
"""

GRAPHS = {
    "video": {
        "prepare": (),
        "audio": ("prepare",),
        "asr": ("audio",),
        "transcript_index": ("asr",),
        "vision": ("prepare",),
        "publish": ("transcript_index", "vision"),
        "project": ("publish",),
    },
    "transcript": {
        "prepare": (),
        "audio": ("prepare",),
        "asr": ("audio",),
        "transcript_index": ("asr",),
        "publish": ("transcript_index",),
        "project": ("publish",),
    },
    # Study notes: model drafts per batch (batch checkpoints), then an idempotent publication.
    "notes": {
        "notes_draft": (),
        "notes_publish": ("notes_draft",),
        "project": ("notes_publish",),
    },
    # Export of a finished study artifact into object storage (download by reference).
    "export": {
        "export": (),
    },
}
# publish decides the outcome (ledger); project then applies the committed decision to the read model and
# runs whatever the outcome (also after a failed or scanner-exhausted publish).
JOIN_STAGES = {"publish", "project"}
TERMINAL = {"succeeded", "failed", "skipped"}
# Separate queues so a long ASR or vision run never starves the short control/publish work.
QUEUES = {"prepare": "media", "audio": "media", "vision": "media", "asr": "asr", "transcript_index": "embed",
          "notes_draft": "embed", "publish": "control", "project": "control", "notes_publish": "control",
          "export": "control"}
ALL_QUEUES = ("media", "asr", "embed", "control")
