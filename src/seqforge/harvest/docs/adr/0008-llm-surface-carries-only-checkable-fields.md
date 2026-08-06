# 8. The LLM's output surface carries only fields code can re-check

Two fields were proposed for the model's structured-output surface and both were refused: exact
character offsets, which false-reject a correct extraction because an LLM cannot count characters,
and a `subject` naming which sample a claim is about, which would be an authority with no quote to
check and would silently misfile a permanent fact. The subject is the document instead — each record
level is rendered as its own document, so "which sample" is answered by which file code chose to
send — and a document's role and scope come from how it arrived, never from its contents, because a
filename trigger is spoofable by renaming a download.
