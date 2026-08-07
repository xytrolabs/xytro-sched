#!/bin/bash
# quantize_q3.sh — convert a fine-tuned LFM (HF format) to GGUF and quantize to
# Q3_K_M for the xytro reasoner (minimal memory/CPU footprint).
#
# Requires llama.cpp installed and the model exported to GGUF:
#   sudo pacman -S llama.cpp    (CachyOS/Arch)  or build from source
#
# Usage:
#   ./quantize_q3.sh /path/to/finetuned-lfm  [output-name]
set -e

HF_DIR="${1:?usage: quantize_q3.sh <hf-model-dir> [out-name]}"
OUT="${2:-lfm-reasoner-q3}"
LLAMA_DIR="${LLAMA_CPP_DIR:-/usr/share/llama.cpp}"   # adjust to your install

# 1. HF -> GGUF (f16), then 2. quantize to Q3_K_M.
python3 "$LLAMA_DIR/convert_hf_to_gguf.py" "$HF_DIR" \
    --outfile "$OUT-f16.gguf" --outtype f16
llama-quantize "$OUT-f16.gguf" "$OUT-q3_k_m.gguf" Q3_K_M

echo
echo "Done. Use it as the reasoner:"
echo "  sudo python3 agent/xytro_agent.py --seconds 20 --live --ab --llm $PWD/$OUT-q3_k_m.gguf"
echo "Sizes:"
ls -lh "$OUT-f16.gguf" "$OUT-q3_k_m.gguf"
