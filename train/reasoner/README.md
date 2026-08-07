# SLM reasoner pipeline (Liquid Foundation Model → Q4 GGUF)

Turns the agent's real decisions into a **small language model** that acts as
the macro-cadence reasoner — choosing a strategy and writing the reason text —
as a quantized **GGUF** for a tiny memory/CPU/GPU footprint.

Target: **Liquid Foundation Model `LFM2.5-230M`** (0.2B), official GGUF repo
`LiquidAI/LFM2.5-230M-GGUF`.

> **Verified 2026-08-06:** the official GGUF repo ships **no Q3_K_M** — only
> F16/BF16/Q4_0/Q4_K_M/Q5_K_M/Q6_K/Q8_0. We run **Q4_0** (143 MB) directly for
> the stock model (~0.6 s/answer on the RTX 4060). For the **fine-tuned**
> reasoner we use the merged HuggingFace model directly via `generate_reason.py`
> (no GGUF conversion needed).

## The steps

### 1. Install tooling
```sh
sudo pacman -S llama-cpp            # package is `llama-cpp`, NOT `llama.cpp`
# Python deps in a venv (Arch is PEP-668 externally-managed). Use ABSOLUTE paths
# and avoid `source` — the waash shell doesn't expand `~` or support `source`.
python3 -m venv /home/raf/.venvs/lfm
/home/raf/.venvs/lfm/bin/pip install -U pip huggingface_hub
# ML stack (only needed to FINE-TUNE; the runtime GGUF path needs no python ML deps):
/home/raf/.venvs/lfm/bin/pip install torch transformers peft bitsandbytes accelerate datasets
```

### 2. Download the official GGUF (Q4_0)
`huggingface-cli` is deprecated; use the Python API (single file, ~143 MB):

```sh
/home/raf/.venvs/lfm/bin/python -c "from huggingface_hub import hf_hub_download; \
print(hf_hub_download('LiquidAI/LFM2.5-230M-GGUF', 'LFM2.5-230M-Q4_0.gguf', local_dir='/home/raf/lfm'))"
```

### 3. Build the dataset + fine-tune (QLoRA)
```sh
# 1) Augment: 40 real audit examples -> ~3500 high-quality ones (natural
#    reasons + rule-based distillation across every phase):
python3 train/reasoner/augment_dataset.py                 # -> reason_dataset.jsonl

# 2) Download the HF base model:
/home/raf/.venvs/lfm/bin/python -c "from huggingface_hub import snapshot_download; \
print(snapshot_download('LiquidAI/LFM2.5-230M', local_dir='/home/raf/lfm-base'))"

# 3) Fine-tune (QLoRA 4-bit, ~15 min on the RTX 4060) and merge adapters:
/home/raf/.venvs/lfm/bin/python train/reasoner/finetune_lora.py \
    --model /home/raf/lfm-base \
    --data train/reasoner/reason_dataset.jsonl \
    --out train/reasoner/lfm-reasoner-lora --merge
# -> merged model at train/reasoner/lfm-reasoner-lora-merged/
```

### 4. Plug into the agent
`--llm` accepts **either** a GGUF file (via `llama-simple`) **or** a fine-tuned
HuggingFace model directory (via `generate_reason.py` under the ML venv):

```sh
# needs the passwordless sudoers drop-in (systemd/install.sh) so the internal
# steer/top calls don't prompt; then no sudo is required here either:

# stock model (GGUF, lightweight):
python3 agent/xytro_agent.py --seconds 20 --live --ab --llm /home/raf/lfm/LFM2.5-230M-Q4_0.gguf

# OR the fine-tuned model (HF dir):
python3 agent/xytro_agent.py --seconds 20 --live --ab \
    --llm train/reasoner/lfm-reasoner-lora-merged
```

### 4b. Quantize further (optional)
```sh
bash train/reasoner/quantize_q3.sh <hf-model-dir> lfm-reasoner-q3   # -> *q3_k_m.gguf
```

## Runtime behavior (agent --llm)
- **GGUF path:** the agent runs `llama-simple` (the one-shot example binary —
  **not** `llama-cli`, which drops into an interactive REPL and hangs under
  subprocess), fully detached (`stdin=/dev/null`, new session), capturing
  **stdout only** (stderr carries non-UTF-8 progress logs).
- **HF-dir path:** if `--llm` points to a directory, the agent runs
  `train/reasoner/generate_reason.py` under the ML venv python
  (`XYTRO_LFM_VENV_PY`, default `~/.venvs/lfm/bin/python`) to load the exact
  fine-tuned weights.
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
- `augment_dataset.py` — expand to ~3500 high-quality examples (natural reasons,
  rule-based distillation, per-phase coverage)
- `finetune_lora.py` — QLoRA fine-tune LFM2.5-230M (transformers/peft/bnb) + merge
- `generate_reason.py` — one-shot generation from a fine-tuned HF model dir
- `quantize_q3.sh` — HF → GGUF → Q3_K_M (auto-detects llama.cpp tools)
- `agent/xytro_agent.py --llm` — the runtime hook (GGUF via llama-simple, or
  HF-dir via the venv; rule-based fallback always)
