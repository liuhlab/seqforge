# PROTOTYPE, throwaway: one tiny call to each model through the gateway.
import json
import time

from labllm import Gateway, Model

gw = Gateway(repo="seqforge")
with gw.meter("prototype bounding smoke", ceiling_usd=0.02) as m:
    t = time.time()
    r = m.chat(
        Model("deepseek", "deepseek-flash", thinking=False, max_output_tokens=16),
        user="Reply with the single word OK.",
    )
    print("deepseek", repr(r.text), r.usage, r.cost_usd, r.cached, round(time.time() - t, 2))
    t = time.time()
    j = m.jev(
        Model("typesafe", "jev-1.14.0"),
        description="A dog is an animal.",
        questions={"q": {"type": "noul", "instructions": "Is a dog an animal?"}},
    )
    print("jev", json.dumps(j.answers), j.usage, j.cost_usd, round(time.time() - t, 2))
    print("spent", m.spent_usd)
