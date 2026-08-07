#!/usr/bin/env python3
"""
finetune_lora.py — QLoRA fine-tune of LiquidAI/LFM2.5-230M on the xytro reason
dataset, so the SLM reasoner picks strategies + writes natural reasons instead
of the base model.

Usage (inside the ML venv, GPU present):
  source /home/raf/.venvs/lfm/bin/activate
  python3 train/reasoner/finetune_lora.py \
      --model LiquidAI/LFM2.5-230M \
      --data train/reasoner/reason_dataset.jsonl \
      --out train/reasoner/lfm-reasoner-lora
"""
import argparse
import json
import os
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="LiquidAI/LFM2.5-230M")
    ap.add_argument("--data", default="train/reasoner/reason_dataset.jsonl")
    ap.add_argument("--out", default="train/reasoner/lfm-reasoner-lora")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--grad-accum", type=int, default=4)
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--lora-dropout", type=float, default=0.05)
    ap.add_argument("--merge", action="store_true",
                    help="merge LoRA into the base and save full model")
    args = ap.parse_args()

    import torch
    from datasets import load_dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
        Trainer, TrainingArguments, DataCollatorForSeq2Seq,
    )

    print("== loading tokenizer ==")
    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def build_prompt(messages):
        if hasattr(tok, "apply_chat_template") and tok.chat_template:
            return tok.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=False)
        # manual LFM2.5 fallback
        out = "<|startoftext|>"
        for m in messages:
            if m["role"] == "system":
                out += "<|im_start|>system\n%s<|im_end|>\n" % m["content"]
            elif m["role"] == "user":
                out += "<|im_start|>user\n%s<|im_end|>\n" % m["content"]
            else:
                out += "<|im_start|>assistant\n%s<|im_end|>\n" % m["content"]
        out += "<|im_start|>assistant\n"
        return out

    def tokenize_fn(batch):
        prompts, labels = [], []
        for msgs in batch["messages"]:
            full = build_prompt(msgs)
            user_msgs = msgs[:-1]
            prompt_part = build_prompt(user_msgs)
            t_full = tok(full, truncation=True, max_length=args.max_len)
            t_prompt = tok(prompt_part, truncation=True, max_length=args.max_len)
            ids = t_full["input_ids"]
            lab = [-100] * len(t_prompt["input_ids"]) + ids[len(t_prompt["input_ids"]):]
            # pad/truncate to max_len
            ids = ids[:args.max_len] + [tok.pad_token_id] * max(0, args.max_len - len(ids))
            lab = lab[:args.max_len] + [-100] * max(0, args.max_len - len(lab))
            prompts.append(ids)
            labels.append(lab)
        return {"input_ids": prompts, "labels": labels}

    print("== loading dataset ==")
    ds = load_dataset("json", data_files=args.data, split="train")
    ds = ds.map(tokenize_fn, batched=True, remove_columns=ds.column_names)

    print("== loading model (4-bit QLoRA) ==")
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model, quantization_config=bnb, device_map="auto",
        trust_remote_code=True, dtype=torch.bfloat16)
    model = prepare_model_for_kbit_training(model)

    lora = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout, bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    args_out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(args_out)), exist_ok=True)

    training_args = TrainingArguments(
        output_dir=args_out,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        bf16=True,
        logging_steps=10,
        save_strategy="epoch",
        save_total_limit=1,
        report_to=[],
        dataloader_pin_memory=False,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=ds,
        data_collator=DataCollatorForSeq2Seq(tok, pad_to_multiple_of=8, return_tensors="pt"),
    )
    trainer.train()

    if args.merge:
        print("== merging LoRA into base ==")
        merged = model.merge_and_unload()
        merged.save_pretrained(args_out + "-merged")
        tok.save_pretrained(args_out + "-merged")
        print("saved merged model -> %s-merged" % args_out)
    else:
        model.save_pretrained(args_out)
        tok.save_pretrained(args_out)
        print("saved LoRA adapters -> %s" % args_out)


if __name__ == "__main__":
    main()
