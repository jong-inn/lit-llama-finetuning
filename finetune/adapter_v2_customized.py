"""
Instruction-tuning with LLaMA-Adapter v2 on the Alpaca dataset following the paper

LLaMA-Adapter V2: Parameter-Efficient Visual Instruction Model
https://arxiv.org/abs/2304.15010

This script runs on a single GPU by default. You can adjust the `micro_batch_size` to fit your GPU memory.
You can finetune within 1 hour as done in the original paper using DeepSpeed Zero-2 on 8 A100 GPUs by setting the
devices variable to `devices = 8` and `micro_batch_size = 8` (or higher).

Note: If you run into a CUDA error "Expected is_sm80 to be true, but got false", uncomment the line
`torch.backends.cuda.enable_flash_sdp(False)` in the script below (see https://github.com/Lightning-AI/lit-llama/issues/101).
"""
import os
import sys
import time
from pathlib import Path
import shutil

import lightning as L
import numpy as np
import torch
import torch.nn as nn
import wandb

# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))

from generate import generate
from lit_llama.adapter import LLaMA, LLaMAConfig
from lit_llama.adapter_v2 import (
    mark_only_adapter_v2_as_trainable,
    add_adapter_v2_parameters_to_linear_layers,
    adapter_v2_state_from_state_dict
    )
from lit_llama.tokenizer import Tokenizer
from scripts.prepare_alpaca import generate_prompt
from lightning.fabric.strategies import DeepSpeedStrategy

save_interval = 1000
eval_iters = 100
log_interval = 50 # Changed
devices = 1 # Changed

"""
Early Stopping
"""

best_val_loss = float('inf')
patience = 8  # Number of evaluation intervals to wait before stopping if no improvement
no_improve_counter = 0

"""
batch size = micro_batch_size * data_parallel_size * gradient_accumulation_steps.

"""

# Hyperparameters
learning_rate = 9e-3
batch_size = 64 / devices
micro_batch_size = 2 # Changed
gradient_accumulation_iters = batch_size // micro_batch_size
assert gradient_accumulation_iters > 0
epoch_size = 1000  # train dataset size

max_epochs = 50 # Added
# num_epochs = 5

# max_iters = num_epochs * (epoch_size // micro_batch_size) // devices
max_iters = max_epochs * (epoch_size // micro_batch_size) // devices # Added
weight_decay = 0.02
max_seq_length = 256  # see scripts/prepare_alpaca.py
warmup_iters = 2 * (epoch_size // micro_batch_size) // devices  # 2 epoch
required_iters = 1 * (epoch_size // micro_batch_size) // devices # Added
# eval_interval = 1 * (epoch_size // micro_batch_size) // devices # Changed, validation_steps
eval_interval = 16 #Eval Approx twice per epoch

ds_config = {
    "train_micro_batch_size_per_gpu": micro_batch_size,
    "gradient_accumulation_steps": gradient_accumulation_iters,
    "zero_optimization": {"stage": 2},
}


def main(
    data_dir: str = "data/alpaca", 
    pretrained_path: str = "checkpoints/lit-llama/7B/lit-llama.pth",
    out_dir: str = "out/adapter_v2/alpaca",
):

    wandb.init(
        project = "Lit LLAMA Instruction FineTuning",
        config = {
            "learning_rate":learning_rate,
            "batch_size": batch_size,
            "epoch_size": epoch_size,
            "num_epochs": max_epochs
        }
    )

    fabric = L.Fabric(
        accelerator="cuda",
        devices=1,
        strategy=(DeepSpeedStrategy(config=ds_config) if devices > 1 else "auto"),
        precision="bf16-true",
    )
    fabric.launch()
    fabric.seed_everything(1337 + fabric.global_rank)

    if fabric.global_rank == 0:
        os.makedirs(out_dir, exist_ok=True)

    train_data, val_data = load_datasets(data_dir=data_dir)

    config = LLaMAConfig(block_size=max_seq_length)

    if not os.path.isfile(pretrained_path):
        raise FileNotFoundError(
            f"Can't find the pretrained weights at {pretrained_path}."
            " Please follow the instructions in the README to download them."
        )
    checkpoint = torch.load(pretrained_path)

    with fabric.init_module():
        model = LLaMA(config)
        # strict=False because missing keys due to adapter weights not contained in state dict
        model.load_state_dict(checkpoint, strict=False)

    add_adapter_v2_parameters_to_linear_layers(model)
    mark_only_adapter_v2_as_trainable(model)

    num_params = sum([p.numel() for p in model.parameters() if p.requires_grad])
    print(f"Number of trainable parameters: {num_params}")

    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    model, optimizer = fabric.setup(model, optimizer)
    train(fabric, model, optimizer, train_data, val_data, out_dir)

    # Save the final checkpoint at the end of training
    save_model_checkpoint(fabric, model, os.path.join(out_dir, "lit-llama-adapter-finetuned.pth"))

    wandb.finish()


def train(
    fabric: L.Fabric,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    train_data: np.ndarray,
    val_data: np.ndarray,
    out_dir: str,
) -> None:
    """The training loop.

    Loosely based on the nanoGPT implementation: https://github.com/karpathy/nanoGPT.
    """
    # Early Stopper
    global best_val_loss
    global no_improve_counter
    
    step_count = 0

    for iter_num in range(max_iters):

        if step_count <= warmup_iters:
            # linear warmup
            lr = learning_rate * step_count / warmup_iters
            for param_group in optimizer.param_groups:
                param_group['lr'] = lr

        t0 = time.time()

        input_ids, targets = get_batch(fabric, train_data)
        with fabric.no_backward_sync(model, enabled=((iter_num + 1) % gradient_accumulation_iters != 0)):
            logits = model(input_ids)
            loss = loss_fn(logits, targets)
            fabric.backward(loss / gradient_accumulation_iters)
        
        # wandb.log({"loss": loss.item()})
        #Figure THIS OUT!!! 

        #mb = 2                                       mb=4
        #32 iters = 64 batch processed                16 iters = 64 batch processed
        #500 iters = 1 Epoch Size processed           250 iters = 1 Epoch Size processed
        #2450 iters = 5 epoch nums                    1000 iters = 1 Epoch Size processed

        #15.1625 step counts = 1 epoch, 77 step counts = 5 epochs  || 15. step counts = 1 epoch. 77 step counts = 5 epochs

        #Mini Batch Size More = Step Counts More (less iters to cover entire 64 batch)
        # Why does val step occur only once % 500 for mb = 2 and once % 125 for mb = 8? It seems eval_interval
        # is fixed in the og code. occuring twice for 3 epochs. 
        #Total Val steps remains the same. normally for mb=64, step count = 1, for mb=2, step count=32
        #for mb larger, max_iters would be less, step counts would be quicker.
        #for mb smaller, max_iters would be more, step counts would be slower.
        #Rate for eval remains the same. 

        #WANDB: Each Step is the processing of one batch of 64 examples

        prev_loss = np.Inf
        if (iter_num + 1) % gradient_accumulation_iters == 0:
            optimizer.step()
            optimizer.zero_grad()
            step_count += 1

            wandb.log({"loss": loss.item()})
                
            if step_count % eval_interval == 0:
                val_loss = validate(fabric, model, val_data)
                
                fabric.print(f"step {iter_num}: val loss {val_loss:.4f}")
                fabric.barrier()
                # Early Stopping
                if step_count % eval_interval == 0:
                    val_loss = validate(fabric, model, val_data)
                    fabric.print(f"step {iter_num}: val loss {val_loss:.4f}")
                    fabric.barrier()

                    # Early stopping check
                    if val_loss < best_val_loss:
                        best_val_loss = val_loss
                        no_improve_counter = 0
                    else:
                        no_improve_counter += 1

                    if no_improve_counter >= patience:
                        fabric.print("Early stopping due to no improvement in validation loss")
                        break

            if step_count % save_interval == 0:
                print(f"Saving adapter weights to {out_dir}")
                # TODO: Provide a function/script to merge the adapter weights with pretrained weights
                save_model_checkpoint(fabric, model, os.path.join(out_dir, f"iter-{iter_num:06d}.pth"))

        dt = time.time() - t0
        if iter_num % log_interval == 0:
            fabric.print(f"iter {iter_num}: loss {loss.item():.4f}, time: {dt*1000:.2f}ms")


def generate_response(model, instruction, input=""):
    tokenizer = Tokenizer("checkpoints/lit-llama/tokenizer.model")
    sample = {"instruction": instruction, "input": input}
    prompt = generate_prompt(sample)
    encoded = tokenizer.encode(prompt, bos=True, eos=False, device=model.device)

    output = generate(
        model,
        idx=encoded,
        max_seq_length=max_seq_length,
        max_new_tokens=100,
        temperature=0.8,
    )
    output = tokenizer.decode(output)
    return output # output.split("### Response:")[1].strip()


@torch.no_grad()
def validate(fabric: L.Fabric, model: torch.nn.Module, val_data: np.ndarray) -> torch.Tensor:
    fabric.print("Validating ...")
    model.eval()
    losses = torch.zeros(eval_iters)
    for k in range(eval_iters):
        input_ids, targets = get_batch(fabric, val_data)
        logits = model(input_ids)
        loss = loss_fn(logits, targets)
        losses[k] = loss.item()
    val_loss = losses.mean()
    wandb.log({"val_loss": val_loss.item()})

    # produce an example:
    instruction = "Recommend a movie for me to watch during the weekend and explain the reason."
    output = generate_response(model, instruction)
    fabric.print(instruction)
    fabric.print(output)

    model.train()
    return val_loss.item()

def loss_fn(logits, targets):
    # shift the targets such that output n predicts token n+1
    logits = logits[..., :-1, :].contiguous()
    targets = targets[..., 1:].contiguous()
    loss = torch.nn.functional.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)
    return loss
    

def get_batch(fabric: L.Fabric, data: list):
    ix = torch.randint(len(data), (micro_batch_size,))

    input_ids = [data[i]["input_ids"].type(torch.int64) for i in ix]
    labels = [data[i]["labels"].type(torch.int64) for i in ix]

    max_len = max(len(s) for s in input_ids)

    def pad_right(x, pad_id):
        # pad right based on the longest sequence
        n = max_len - len(x)
        return torch.cat((x, torch.full((n,), pad_id, dtype=x.dtype)))

    x = torch.stack([pad_right(x, pad_id=0) for x in input_ids])
    y = torch.stack([pad_right(x, pad_id=-1) for x in labels])
    x, y = fabric.to_device((x.pin_memory(), y.pin_memory()))
    return x, y


def load_datasets(data_dir):
    train_data = torch.load(os.path.join(data_dir, "train.pt"))
    val_data = torch.load(os.path.join(data_dir, "validation.pt"))
    return train_data, val_data


def save_model_checkpoint(fabric, model, file_path):
    file_path = Path(file_path)

    if isinstance(fabric.strategy, DeepSpeedStrategy):
        from deepspeed.utils.zero_to_fp32 import get_fp32_state_dict_from_zero_checkpoint

        tmp_path = file_path.with_suffix(".tmp")
        fabric.save(tmp_path, {"model": model})
        fabric.barrier()
        if fabric.global_rank == 0:
            # Create a consolidated checkpoint with the same name next to the deepspeed checkpoint
            # and only keep the adapter weights
            state_dict = get_fp32_state_dict_from_zero_checkpoint(tmp_path)
            state_dict = adapter_v2_state_from_state_dict(state_dict)
            torch.save(state_dict, file_path)
            shutil.rmtree(tmp_path)
    else:
        state_dict = adapter_v2_state_from_state_dict(model.state_dict())
        if fabric.global_rank == 0:
            torch.save(state_dict, file_path)
        fabric.barrier()


if __name__ == "__main__":
    # Uncomment this line if you see an error: "Expected is_sm80 to be true, but got false"
    # torch.backends.cuda.enable_flash_sdp(False)
    torch.set_float32_matmul_precision("high")

    from jsonargparse.cli import CLI

    CLI(main)
