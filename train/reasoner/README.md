# SLM reasoner pipeline (Liquid Foundation Model → Q3 GGUF)

Turns the agent's real decisions into a fine-tuned **small language model**
that acts as the macro-cadence reasoner — choosing a strategy and writing the
`$reason` text — with a **Q3-quantized GGUF** for minimal memory/CPU footprint.

Target: **Liquid Foundation Model, LFM2.5-350M** (0.4B) as the *minimal* choice, or **LFM2-1.2B** if you want more reliable format-following. Liquid's lineup goes all the way down to **230M** (plus task-specific "Liquid Nanos"), and there are **official GGUF releases** (e.g. `LiquidAI/LFM2.5-2.6B-GGUF`), so llama.cpp compatibility is a given and a Q3 GGUF may already exist on Hugging Face — check before converting.

## The four steps

### 1. Install tooling
```sh
sudo pacman -S llama.cpp python-llama-cpp   # llama-cli, llama-quantize, convert script
pip install datasets transformers peft bitsandbytes accelerate
```

### 2. Pick + download the base LFM
Liquid AI hosts models on Hugging Face (`LiquidAI/`). Current lineup includes
LFM2.5-**230M**, **350M**, **2.6B**, **8B-A1B**, plus task-specific **Liquid Nanos**
(LFM2-350M-Extract, LFM2-1.2B-RAG, …). For a minimal reasoner, try
**LFM2.5-350M**; for better format reliability, **LFM2-1.2B**.

```sh
pip install huggingface_hub
# either the raw model (to fine-tune), or an official GGUF if one exists at Q3:
huggingface-cli download LiquidAI/LFM2.5-350M --local-dir lfm-base
# GGUF variant (if present): huggingface-cli download LiquidAI/LFM2.5-350M-GGUF --local-dir lfm-gguf
```

### 3. Build the dataset + fine-tune (QLoRA)
```sh
python3 train/reasoner/build_reason_dataset.py          # -> reason_dataset.jsonl
```
Then QLoRA fine-tune `LiquidAI/LFM-1B` on that JSONL (chat format) with
`transformers` + `peft` + `bitsandbytes` — a standard QLoRA run. On the RTX 4060
(8 GB VRAM) a 1B model fine-tune is ~30–60 min.

### 4. Convert → quantize to Q3 → plug in
```sh
bash train/reasoner/quantize_q3.sh lfm-1b-finetuned lfm-reasoner-q3
# produces lfm-reasoner-q3_q3_k_m.gguf
sudo python3 agent/xytro_agent.py --seconds 20 --live --ab --llm ./lfm-reasoner-q3_q3_k_m.gguf
```

The agent calls `llama-cli` with a telemetry summary, parses
`STRATEGY=… REASON=…`, and **falls back to the deterministic rule-based
reasoner** if the model is missing, slow, or errors — so this is purely an
upgrade, never a single point of failure.

## Why Q3 (and the honest trade-off)
- **Q3_K_M** is the smallest practical GGUF level → minimal RAM/CPU for a
  background reasoner that wakes every ~30–60 s.
- If you care more about answer quality than minimal footprint, **Q4_K_M or
  Q5_K_M** preserve accuracy noticeably better while still being small
  (LFM-1B at Q4 is ~0.8–1 GB). The agent integration is identical either way.

## Files
- `build_reason_dataset.py` — audit log → chat-JSONL training pairs
- `quantize_q3.sh` — HF → GGUF → Q3_K_M
- `agent/xytro_agent.py --llm` — the runtime hook (rule-based fallback built in)
