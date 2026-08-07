#!/usr/bin/env python3
"""
generate_reason.py — one-shot generation from the fine-tuned xytro reasoner.

Runs under the ML venv (has transformers + torch). The prompt is the ALREADY
chat-templated string built by the agent (ends with "<|im_start|>assistant\n"),
so we tokenize it as-is and print the raw continuation (STRATEGY=... REASON=...).

Usage:
  ~/.venvs/lfm/bin/python generate_reason.py --model <hf-dir> --prompt "<text>"
"""
import argparse
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--max-new", type=int, default=96)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.model, device_map="auto", trust_remote_code=True,
        torch_dtype=torch.bfloat16)
    model.eval()

    pad = tok.pad_token_id or tok.eos_token_id
    ids = tok(args.prompt, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=args.max_new,
                             do_sample=False, pad_token_id=pad)
    gen = tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)
    print(gen)


if __name__ == "__main__":
    main()
