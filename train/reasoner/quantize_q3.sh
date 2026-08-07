#!/bin/bash
# quantize_q3.sh — convert a fine-tuned LFM (HF format) to GGUF and quantize to
# Q3_K_M for the xytro reasoner (minimal memory/CPU footprint).
#
# Install llama.cpp first (the Arch/CachyOS package is named `llama-cpp`):
#   sudo pacman -S llama-cpp
#
# NOTE: if you downloaded an official GGUF (e.g. LiquidAI/LFM2.5-230M-GGUF) and
# it already has a Q3_K_M file, you can skip this whole script.
#
# Usage:
#   ./quantize_q3.sh /path/to/finetuned-lfm  [output-name]
set -e

HF_DIR="${1:?usage: quantize_q3.sh <hf-model-dir> [out-name]}"
OUT="${2:-lfm-reasoner-q3}"

# Locate llama.cpp tools (Arch installs them in /usr/bin).
LLAMA_QUANTIZE="$(command -v llama-quantize || true)"
CONVERT="$(command -v convert-hf-to-gguf || true)"
if [ -z "$CONVERT" ] && [ -f /usr/share/llama.cpp/convert_hf_to_gguf.py ]; then
    CONVERT="python3 /usr/share/llama.cpp/convert_hf_to_gguf.py"
fi
if [ -z "$LLAMA_QUANTIZE" ]; then
    echo "ERROR: llama-quantize not found. Install it: sudo pacman -S llama-cpp"
    exit 1
fi
if [ -z "$CONVERT" ]; then
    echo "ERROR: convert_hf_to_gguf not found. Install it: sudo pacman -S llama-cpp"
    exit 1
fi

# 1. HF -> GGUF (f16), then 2. quantize to Q3_K_M.
$CONVERT "$HF_DIR" --outfile "$OUT-f16.gguf" --outtype f16
"$LLAMA_QUANTIZE" "$OUT-f16.gguf" "$OUT-q3_k_m.gguf" Q3_K_M

echo
echo "Done. Use it as the reasoner:"
echo "  sudo python3 agent/xytro_agent.py --seconds 20 --live --ab --llm $PWD/$OUT-q3_k_m.gguf"
echo "Sizes:"
ls -lh "$OUT-f16.gguf" "$OUT-q3_k_m.gguf"
