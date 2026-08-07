#!/usr/bin/env python3
"""
build_reason_dataset.py — build SLM fine-tuning data from real agent decisions.

Reads the agent's append-only audit log(s) and turns each observed decision into
a (telemetry -> strategy + reason) training pair in chat JSONL format, so a
pretrained LFM can be fine-tuned to be the reasoner.

Output (default train/reasoner/reason_dataset.jsonl), one JSON object per line:
  {"messages": [{"role":"system","content":...},
                {"role":"user","content":"Telemetry: ..."},
                {"role":"assistant","content":"STRATEGY=... REASON=..."}]}
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_AUDIT = os.path.join(HERE, "..", "..", "agent", "audit.log")
DEFAULT_OUT = os.path.join(HERE, "reason_dataset.jsonl")

SYSTEM = ("You are xytro-agent, the AI steersman of a CPU scheduler. "
          "Given telemetry, choose exactly one strategy from "
          "{interactive, throughput, balanced, power} and give a one-sentence "
          "reason.")


def entry_to_examples(entry):
    """Yield (user_prompt, assistant_reply) from one audit entry, if usable."""
    if not isinstance(entry, dict):
        return
    strat = entry.get("strategy")
    if strat not in ("interactive", "throughput", "balanced", "power"):
        return
    m = entry.get("metrics") or {}
    phase = entry.get("phase", "unknown")
    prompt = ("Telemetry: decisions=%s dec/s=%s fast_ratio=%s "
              "latency_p50=%s p90=%s p99=%s phase=%s" % (
                  m.get("dec"), m.get("dec_per_s"), m.get("fast_ratio"),
                  m.get("p50_us"), m.get("p90_us"), m.get("p99_us"), phase))
    reason = entry.get("note") or ("%s selected for %s phase"
                                   % (strat, phase))
    reply = "STRATEGY=%s REASON=%s" % (strat, reason)
    yield prompt, reply


def main():
    ap = argparse.ArgumentParser(description="Build SLM reasoner training data")
    ap.add_argument("--audits", nargs="*", default=[AGENT_AUDIT],
                    help="agent audit JSONL files to read")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    n = 0
    with open(args.out, "w") as out:
        for path in args.audits:
            if not os.path.exists(path):
                print("skip (missing): %s" % path)
                continue
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for prompt, reply in entry_to_examples(entry):
                        out.write(json.dumps({
                            "messages": [
                                {"role": "system", "content": SYSTEM},
                                {"role": "user", "content": prompt},
                                {"role": "assistant", "content": reply},
                            ]
                        }) + "\n")
                        n += 1
    print("wrote %d examples to %s" % (n, args.out))


if __name__ == "__main__":
    main()
