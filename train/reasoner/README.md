# SLM reasoner pipeline (Liquid Foundation Model → Q4 GGUF)

Turns the agent's real decisions into a **small language model** that acts as
the macro-cadence reasoner — choosing a strategy and writing the reason text —
as a quantized **GGUF** for a tiny memory/CPU/GPU footprint.

Target: **Liquid Foundation Model `LFM2.5-230M`** (0.2B), official GGUF repo
`LiquidAI/LFM2.5-230M-GGUF`.

> **Verified 2026-08-06:** the official GGUF repo ships **no Q3_K_M** — only
> F16/BF16/Q4_0/Q4_K_M/Q5_K_M/Q6_K/Q8_0. We run **Q4_0** (143 MB) directly,
> which on the RTX 4060 (Vulkan) answers in ~0.6 s — no quantization needed.
> Quantizing further is optional (see step 4b).

## The steps

### 1. Install tooling
```sh
sudo pacman -S llama-cpp            # package is `llama-cpp`, NOT `llama.cpp`
# Python deps in a venv (Arch is PEP-668 externally-managed). Use ABSOLUTE paths
# and avoid `source` — the waash shell doesn't expand `~` or support `source`.
python3 -m venv /home/raf/.venvs/lfm
/home/raf/.venvs/lfm/bin/pip install -U pip huggingface_hub
```

### 2. Download the official GGUF (Q4_0)
`huggingface-cli` is deprecated; use the Python API (single file, ~143 MB):

```sh
/home/raf/.venvs/lfm/bin/python -c "from huggingface_hub import hf_hub_download; \
print(hf_hub_download('LiquidAI/LFM2.5-230M-GGUF', 'LFM2.5-230M-Q4_0.gguf', local_dir='/home/raf/lfm'))"
```

### 3. Build the dataset + fine-tune (optional, QLoRA)
```sh
python3 train/reasoner/build_reason_dataset.py          # -> reason_dataset.jsonl
```
QLoRA fine-tune `LiquidAI/LFM2.5-230M` on that JSONL with `transformers` +
`peft` + `bitsandbytes`. On the RTX 4060 (8 GB VRAM) a 0.2B fine-tune is quick.

### 4. Plug into the agent
```sh
# needs the passwordless sudoers drop-in (systemd/install.sh) so the internal
# steer/top calls don't prompt; then no sudo is required here either:
python3 agent/xytro_agent.py --seconds 20 --live --ab --llm /home/raf/lfm/LFM2.5-230M-Q4_0.gguf
```

### 4b. Quantize further (only after fine-tuning)
```sh
bash train/reasoner/quantize_q3.sh <hf-model-dir> lfm-reasoner-q3   # -> *q3_k_m.gguf
```

## Runtime behavior (agent --llm)
The agent runs `llama-simple` (the one-shot example binary — **not** `llama-cli`,
which drops into an interactive REPL and hangs under subprocess):
- prompt is wrapped in the **LFM2.5 chat template**
  (`<|startoftext|><|im_start|>user …<|im_end|>\n<|im_start|>assistant`) plus a
  **few-shot example** so the 0.2B model copies the exact output format;
- runs detached (`stdin=/dev/null`, new session), captures **stdout only**
  (stderr carries non-UTF-8 progress logs);
- parses `STRATEGY=`/`REASON=`/`Explanation:` and falls back to scanning for a
  strategy word in prose;
- if the model is missing, slow, or errors it **falls back to the deterministic
  rule-based reasoner** — this is purely an upgrade, never a single point of failure.

## Why Q4_0 (and the honest trade-off)
- **Q4_0** is the smallest official quant in the repo (143 MB); on a 46 GB RAM
  box + RTX 4060 it's effectively free and fast (~0.6 s/decision).
- If you want better answer quality, **Q4_K_M / Q5_K_M** preserve accuracy
  better while still small. The agent integration is identical either way.

## Files
- `build_reason_dataset.py` — audit log → chat-JSONL training pairs
- `quantize_q3.sh` — HF → GGUF → Q3_K_M (auto-detects llama.cpp tools; only
  needed if you fine-tune)
- `agent/xytro_agent.py --llm` — the runtime hook (`llama-simple` + rule-based fallback)
