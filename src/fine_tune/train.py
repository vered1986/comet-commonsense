"""
Fine-tuning the library models for language modeling on a text file using a causal language modeling (CLM) loss
on the part we want to generate (RoT).
This file is adapted from https://github.com/huggingface/transformers/blob/master/examples/run_lm_finetuning.py
"""
import os
import re
import glob
import torch
import random
import shutil
import pickle
import logging
import argparse
import numpy as np
import utils.utils as utils
import src.data.config as cfg

from tqdm import tqdm, trange
from torch.nn import CrossEntropyLoss
from torch.utils.data import DataLoader, Dataset, RandomSampler, SequentialSampler

from transformers import (AdamW, GPT2Config, GPT2LMHeadModel, GPT2Tokenizer, OpenAIGPTConfig,
                          OpenAIGPTLMHeadModel, OpenAIGPTTokenizer, get_linear_schedule_with_warmup)

from src.fine_tune.common import load_atomic_data_for_training

try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    from tensorboardX import SummaryWriter


logging.basicConfig(format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
                    datefmt="%m/%d/%Y %H:%M:%S", level=logging.INFO)
logger = logging.getLogger(__name__)


MODEL_CLASSES = {
    "gpt2": (GPT2Config, GPT2LMHeadModel, GPT2Tokenizer),
    "openai-gpt": (OpenAIGPTConfig, OpenAIGPTLMHeadModel, OpenAIGPTTokenizer)
}


class TextDataset(Dataset):
    """
    Pairs of sentences: situation [SEP] RoT
    """
    def __init__(self, tokenizer, args, categories, file_path="train", block_size=512):
        assert os.path.isfile(file_path)
        directory, filename = os.path.split(file_path)
        cached_features_file = os.path.join(
            directory, args.model_name_or_path + "_cached_lm_" + str(block_size) + "_" + filename)

        if os.path.exists(cached_features_file) and not args.overwrite_cache:
            logger.info(f"Loading features from cached file {cached_features_file}")
            with open(cached_features_file, "rb") as handle:
                self.examples = pickle.load(handle)
        else:
            logger.info(f"Creating features from dataset file at {directory}")
            self.examples = load_atomic_data_for_training(
                file_path, categories, tokenizer, args.max_input_length, args.max_output_length)

            logger.info(f"Saving features into cached file {cached_features_file}")
            with open(cached_features_file, "wb") as handle:
                pickle.dump(self.examples, handle, protocol=pickle.HIGHEST_PROTOCOL)

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, item):
        return torch.tensor(self.examples[item])


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--train_file", default="v4_atomic_train.csv", type=str,
                        required=True, help="The input training CSV file.")
    parser.add_argument("--out_dir", default=None, type=str, required=True, help="Out directory (predictions/checkpoints).")

    # Other parameters
    parser.add_argument("--adam_epsilon", default=1e-8, type=float, help="Epsilon for Adam optimizer.")
    parser.add_argument("--device", default="cpu", type=str, help="GPU number or 'cpu'.")
    parser.add_argument("--do_eval", action="store_true", help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case", action="store_true", help="Set this flag if you are using an uncased model.")
    parser.add_argument("--do_train", action="store_true", help="Whether to run training.")
    parser.add_argument("--eval_batch_size", default=4, type=int, help="Batch size for evaluation.")
    parser.add_argument("--eval_data_file", default=None, type=str, help="Validation file")
    parser.add_argument("--eval_during_train", action="store_true", help="Evaluate at each train logging step.")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1, help="Steps before backward pass.")
    parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
    parser.add_argument("--logging_steps", type=int, default=1500, help="Log every X updates steps.")
    parser.add_argument("--max_input_length", default=50, type=int, help="Maximum input event length in words.")
    parser.add_argument("--max_output_length", default=50, type=int, help="Maximum output event length in words.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
    parser.add_argument("--max_steps", default=-1, type=int, help="If > 0: total number of training steps to perform.")
    parser.add_argument("--model_name_or_path", default="openai-gpt", type=str, help="LM checkpoint for initialization.")
    parser.add_argument("--model_type", default="openai-gpt", type=str, help="The LM architecture to be fine-tuned.")
    parser.add_argument("--num_train_epochs", default=1.0, type=float, help="Number of training epochs to perform.")
    parser.add_argument("--overwrite_cache", action="store_true", help="Overwrite the cached data.")
    parser.add_argument("--overwrite_out_dir", action="store_true", help="Overwrite the output directory.")
    parser.add_argument("--save_steps", type=int, default=1500, help="Save checkpoint every X updates steps.")
    parser.add_argument("--save_total_limit", type=int, default=None, help="Maximum number of checkpoints to keep")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for initialization.")
    parser.add_argument("--train_batch_size", default=4, type=int, help="Batch size for training.")
    parser.add_argument("--warmup_steps", default=0, type=int, help="Linear warmup over warmup_steps.")
    parser.add_argument("--weight_decay", default=0.0, type=float, help="Weight decay if we apply some.")
    args = parser.parse_args()

    if args.eval_data_file is None and args.do_eval:
        raise ValueError("Cannot do evaluation without an evaluation data file. Either supply --eval_data_file "
                         "or remove the --do_eval argument.")

    if os.path.exists(args.out_dir) and os.listdir(args.out_dir) and args.do_train and not args.overwrite_out_dir:
        raise ValueError(f"Output directory {args.out_dir} already exists and is not empty. Use --overwrite_out_dir.")

    # Setup device
    device = torch.device(f"cuda:{args.device}" if torch.cuda.is_available() and args.device != "cpu" else "cpu")
    args.device = device

    # Set seed
    set_seed(args)

    # Load the models
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(args.model_name_or_path)
    tokenizer = tokenizer_class.from_pretrained(args.model_name_or_path, do_lower_case=args.do_lower_case)
    args.block_size = tokenizer.max_len_single_sentence
    model = model_class.from_pretrained(args.model_name_or_path, config=config)
    model.to(args.device)
    logger.info(f"Training/evaluation parameters {args}")

    # Generate configuration files depending on experiment being run
    utils.generate_config_files("atomic", "0")

    # Loads the correct configuration file
    config_file = "config/atomic/config_0.json"

    # Read config file to option
    config = cfg.read_config(cfg.load_config(config_file))
    opt, meta = cfg.get_parameters(config)

    # Add special tokens
    tokenizer.add_tokens([f"<{cat}>" for cat in opt.data.categories] +
                         ["<blank>", "<eos>", "personx", "persony"])
    model.resize_token_embeddings(len(tokenizer))

    # Training
    if args.do_train:
        train_dataset = load_and_cache_examples(args.train_file, args, tokenizer, opt.data.categories)
        global_step, tr_loss = train(args, opt.data.categories, train_dataset, model, tokenizer)
        logger.info(f" global_step = {global_step}, average loss = {tr_loss}")

        # Create output directory if needed
        if not os.path.exists(args.out_dir):
            os.makedirs(args.out_dir)

        logger.info(f"Saving model checkpoint to {args.out_dir}")

        # Save a trained model, configuration and tokenizer using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        model_to_save = (model.module if hasattr(model, "module") else model)
        model_to_save.save_pretrained(args.out_dir)
        tokenizer.save_pretrained(args.out_dir)

        # Good practice: save your training arguments together with the trained model
        torch.save(args, os.path.join(args.out_dir, "training_args.bin"))

        # Load a trained model and vocabulary that you have fine-tuned
        model = model_class.from_pretrained(args.out_dir)
        tokenizer = tokenizer_class.from_pretrained(args.out_dir, do_lower_case=args.do_lower_case)
        model.to(args.device)

    # Evaluation
    results = {}
    if args.do_eval:
        checkpoint = args.out_dir
        logger.info(f"Evaluate the following checkpoint: {checkpoint}")
        prefix = checkpoint.split("/")[-1] if checkpoint.find("checkpoint") != -1 else ""
        model = model_class.from_pretrained(checkpoint)
        model.to(args.device)
        result = evaluate(args, opt.data.categories, model, tokenizer, prefix=prefix)
        results.update(result)

    return results


def load_and_cache_examples(file_path, args, tokenizer, categories=[]):
    """
    Load the dataset from the cache or from the CSV file
    """
    return TextDataset(tokenizer, args, categories, file_path=file_path, block_size=args.block_size)


def set_seed(args):
    """
    Set the random seed for reproducibility
    """
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)


def _rotate_checkpoints(args, checkpoint_prefix, use_mtime=False):
    """
    Keep a maximum of args.save_total_limit checkpoints.
    """
    if not args.save_total_limit:
        return

    if args.save_total_limit <= 0:
        return

    # Check if we should delete older checkpoint(s)
    glob_checkpoints = glob.glob(os.path.join(args.out_dir, "{}-*".format(checkpoint_prefix)))
    if len(glob_checkpoints) <= args.save_total_limit:
        return

    ordering_and_checkpoint_path = []
    for path in glob_checkpoints:
        if use_mtime:
            ordering_and_checkpoint_path.append((os.path.getmtime(path), path))
        else:
            regex_match = re.match(".*{}-([0-9]+)".format(checkpoint_prefix), path)
            if regex_match and regex_match.groups():
                ordering_and_checkpoint_path.append((int(regex_match.groups()[0]), path))

    checkpoints_sorted = sorted(ordering_and_checkpoint_path)
    checkpoints_sorted = [checkpoint[1] for checkpoint in checkpoints_sorted]
    number_of_checkpoints_to_delete = max(0, len(checkpoints_sorted) - args.save_total_limit)
    checkpoints_to_be_deleted = checkpoints_sorted[:number_of_checkpoints_to_delete]
    for checkpoint in checkpoints_to_be_deleted:
        logger.info("Deleting older checkpoint [{}] due to args.save_total_limit".format(checkpoint))
        shutil.rmtree(checkpoint)


def train(args, categories, train_dataset, model, tokenizer):
    """
    Train the model.
    """
    tb_writer = SummaryWriter()
    train_sampler = RandomSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

    # Set the number of steps based on the num_epochs * len(train) or args.max_steps if specified.
    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

    # Prepare optimizer and scheduler (linear warmup and decay)
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0},
    ]

    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total)

    # Check if saved optimizer or scheduler states exist and load from there
    if os.path.isfile(os.path.join(args.model_name_or_path, "optimizer.pt")) and \
            os.path.isfile(os.path.join(args.model_name_or_path, "scheduler.pt")):
        optimizer.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "optimizer.pt")))
        scheduler.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "scheduler.pt")))

    # Train
    total_batch_size = args.train_batch_size * args.gradient_accumulation_steps
    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size = {args.train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {t_total}")

    global_step = 0
    epochs_trained = 0
    steps_trained_in_current_epoch = 0

    # Check if continuing training from a checkpoint
    if os.path.exists(args.model_name_or_path):
        try:
            # set global_step to global_step of last saved checkpoint from model path
            checkpoint_suffix = args.model_name_or_path.split("-")[-1].split("/")[0]
            global_step = int(checkpoint_suffix)
            epochs_trained = global_step // (len(train_dataloader) // args.gradient_accumulation_steps)
            steps_trained_in_current_epoch = global_step % (len(train_dataloader) // args.gradient_accumulation_steps)

            logger.info("  Continuing training from checkpoint, will skip to saved global_step")
            logger.info(f"  Continuing training from epoch {epochs_trained}")
            logger.info(f"  Continuing training from global step {global_step}")
            logger.info(f"  Will skip the first {steps_trained_in_current_epoch} steps in the first epoch")
        except ValueError:
            logger.info("  Starting fine-tuning.")

    tr_loss, logging_loss = 0.0, 0.0

    model_to_resize = model.module if hasattr(model, "module") else model
    model_to_resize.resize_token_embeddings(len(tokenizer))

    model.zero_grad()
    train_iterator = trange(epochs_trained, int(args.num_train_epochs), desc="Epoch")
    set_seed(args)  # Added here for reproducibility

    for _ in train_iterator:
        epoch_iterator = tqdm(train_dataloader, desc="Iteration")
        for step, batch in enumerate(epoch_iterator):

            # Skip past any already trained steps if resuming training
            if steps_trained_in_current_epoch > 0:
                steps_trained_in_current_epoch -= 1
                continue

            model.train()

            # Take the loss only for the part after the input (as in seq2seq architecture)
            loss = get_loss(args, batch, model)

            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            loss.backward()

            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

                if args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    # Log metrics
                    if args.eval_during_train:
                        results = evaluate(args, categories, model, tokenizer)
                        for key, value in results.items():
                            tb_writer.add_scalar("eval_{}".format(key), value, global_step)
                    tb_writer.add_scalar("lr", scheduler.get_lr()[0], global_step)
                    tb_writer.add_scalar("loss", (tr_loss - logging_loss) / args.logging_steps, global_step)
                    logging_loss = tr_loss

                if args.save_steps > 0 and global_step % args.save_steps == 0:
                    checkpoint_prefix = "checkpoint"

                    # Save model checkpoint
                    out_dir = os.path.join(args.out_dir, "{}-{}".format(checkpoint_prefix, global_step))

                    if not os.path.exists(out_dir):
                        os.makedirs(out_dir)

                    model_to_save = model.module if hasattr(model, "module") else model
                    model_to_save.save_pretrained(out_dir)
                    tokenizer.save_pretrained(out_dir)
                    torch.save(args, os.path.join(out_dir, "training_args.bin"))
                    logger.info("Saving model checkpoint to %s", out_dir)

                    _rotate_checkpoints(args, checkpoint_prefix)

                    torch.save(optimizer.state_dict(), os.path.join(out_dir, "optimizer.pt"))
                    torch.save(scheduler.state_dict(), os.path.join(out_dir, "scheduler.pt"))
                    logger.info("Saving optimizer and scheduler states to %s", out_dir)

            if 0 < args.max_steps < global_step:
                epoch_iterator.close()
                break

        if 0 < args.max_steps < global_step:
            train_iterator.close()
            break

    tb_writer.close()
    return global_step, tr_loss / global_step


def get_loss(args, batch, model):
    """
    Encoder-decoder loss
    """
    inputs, labels = (batch, batch)
    inputs = inputs.to(args.device)
    labels = labels.to(args.device)
    lm_logits = model(inputs)[0]
    shift_logits = lm_logits[..., args.max_input_length:-1, :].contiguous()
    shift_labels = labels[..., args.max_input_length+1  :].contiguous()
    loss_fct = CrossEntropyLoss()
    loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
    return loss


def evaluate(args, categories, model, tokenizer, prefix=""):
    """
    Evaluation
    """
    eval_out_dir = args.out_dir
    eval_dataset = load_and_cache_examples(args.eval_data_file, args, tokenizer, categories)

    if not os.path.exists(eval_out_dir):
        os.makedirs(eval_out_dir)

    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

    logger.info(f"***** Running evaluation {prefix} *****")
    logger.info(f"  Num examples = {len(eval_dataset)}")
    logger.info(f"  Batch size = {args.eval_batch_size}")
    eval_loss = 0.0
    nb_eval_steps = 0
    model.eval()

    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        with torch.no_grad():
            lm_loss = get_loss(args, batch, model)
            eval_loss += lm_loss.mean().item()
        nb_eval_steps += 1

    eval_loss = eval_loss / nb_eval_steps
    perplexity = torch.exp(torch.tensor(eval_loss))

    result = {"perplexity": perplexity}

    output_eval_file = os.path.join(eval_out_dir, prefix, "eval_results.txt")
    with open(output_eval_file, "w") as writer:
        logger.info(f"***** Eval results {prefix} *****")
        for key in sorted(result.keys()):
            logger.info(f"  {key} = {result[key]}")
            writer.write(f"{key} = {result[key]}\n")

    return result


if __name__ == "__main__":
    main()
