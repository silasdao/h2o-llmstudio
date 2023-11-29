import gc
import logging
import os
import re
import shutil
from collections import OrderedDict
from typing import Any, Dict

import coolname
import deepspeed
import numpy as np
import torch
from deepspeed.runtime.dataloader import DeepSpeedDataLoader
from deepspeed.utils.zero_to_fp32 import get_fp32_state_dict_from_zero_checkpoint
from peft import LoraConfig, get_peft_model
from torch.cuda.amp import autocast
from torch.nn.parallel import DistributedDataParallel
from tqdm import tqdm
from transformers import (
    AutoConfig,
    AutoModel,
    BitsAndBytesConfig,
    GenerationMixin,
    StoppingCriteria,
    StoppingCriteriaList,
)
from transformers.utils import logging as transformers_logging

from llm_studio.src.datasets.text_utils import get_tokenizer
from llm_studio.src.optimizers import Optimizers
from llm_studio.src.schedulers import Schedulers
from llm_studio.src.utils.data_utils import (
    OrderedDistributedSampler,
    batch_padding,
    cat_batches,
    get_inference_batch_size,
)
from llm_studio.src.utils.exceptions import LLMDataException, LLMModelException
from llm_studio.src.utils.logging_utils import TqdmToLogger
from llm_studio.src.utils.utils import save_pickle

logger = logging.getLogger(__name__)


def unwrap_model(model: torch.nn.Module):
    options = (torch.nn.parallel.DistributedDataParallel, torch.nn.DataParallel)

    while isinstance(model, options):
        model = model.module

    return model


def check_disk_space(model: torch.nn.Module, path: str, use_deepspeed: bool = False):
    total, used, free = shutil.disk_usage(path)

    model_size_in_bytes = 0
    for param in model.parameters():
        if param.data.dtype in [torch.int8, torch.uint8]:
            model_size_in_bytes += param.numel() * 1
        elif param.data.dtype in [torch.float16, torch.bfloat16]:
            model_size_in_bytes += param.numel() * 2
        elif param.data.dtype == torch.float32:
            model_size_in_bytes += param.numel() * 4
        else:
            # If the data type is not supported, calculate it as float32.
            model_size_in_bytes += param.numel() * 4
            logger.warning(f"Unsupported data type: {param.data.dtype}")

    if use_deepspeed:
        model_size_in_bytes *= 2  # need double space for converting deepspeed engine.
    if model_size_in_bytes * 1.03 < free:  # leave a 3% margin here.
        logger.info("Enough space available for saving model weights.")
    else:
        raise ValueError(
            f"Not enough space available for saving model weights. "
            f"Required space: {model_size_in_bytes * 1.03 / (1024 * 1024):.2f}MB, "
            f"Available space: {free / (1024 * 1024):.2f}MB."
        )


# TODO: currently not saving optimizer
def save_checkpoint(model: torch.nn.Module, path: str, cfg: Any):
    """Saves a model checkpoint if the path is provided.

    Args:
        model: model to save
        path: path to save the checkpoint to

    Returns:
        Dictionary with all the keys to save
    """

    if cfg.environment.use_deepspeed:
        if path is not None:
            # gather model params from all ranks
            model.save_checkpoint(os.path.join(path, "ds_checkpoint"))  # type: ignore[operator] # noqa: E501
            if cfg.environment._local_rank == 0:
                # load to cpu
                state_dict = get_fp32_state_dict_from_zero_checkpoint(
                    os.path.join(path, "ds_checkpoint")
                )
                # save as normal checkpoint that can be loaded by `load_state_dict`
                checkpoint = {"model": state_dict}
                torch.save(checkpoint, os.path.join(path, "checkpoint.pth"))
                shutil.rmtree(os.path.join(path, "ds_checkpoint"))
    elif cfg.environment._local_rank == 0:
        model = unwrap_model(model)
        checkpoint = {"model": model.state_dict()}
        if path is not None:
            torch.save(checkpoint, os.path.join(path, "checkpoint.pth"))

    if (
        cfg.environment._local_rank == 0
        and cfg.problem_type == "text_causal_classification_modeling"
    ):
        torch.save(
            checkpoint["model"]["classification_head.weight"],
            os.path.join(path, "classification_head.pth"),
        )


def load_model_weights(
    model: torch.nn.Module, model_weights: Dict, strict: bool, cfg: Any
):
    orig_num_items = len(model_weights)
    model_state_dict = model.state_dict()

    # needed to load models trained in int4/int8 with other dtypes
    model_weights = {
        k: v
        if not (
            cfg.architecture.backbone_dtype not in ("int4", "int8")
            and (v.dtype is torch.int8 or v.dtype is torch.uint8)
        )
        else model_state_dict[k]
        for k, v in model_weights.items()
        if not (
            ("SCB" in k or "weight_format" in k)
            and cfg.architecture.backbone_dtype not in ("int4", "int8")
        )
    }

    # Need to ignore int4/int8 weights so undo strict loading requirement
    if len(model_weights) != orig_num_items:
        strict = False

    model_weights = {re.sub(r"^module\.", "", k): v for k, v in model_weights.items()}
    model_weights = {k.replace("_orig_mod.", ""): v for k, v in model_weights.items()}

    # manual fix for int8 weights
    if cfg.architecture.backbone_dtype == "int8":
        model_weights = {
            k: v.to(cfg.environment._device) if "weight_format" not in k else v
            for k, v in model_weights.items()
        }

    try:
        model.load_state_dict(OrderedDict(model_weights), strict=True)
    except Exception as e:
        if strict:
            raise e
        if cfg.environment._local_rank == 0:
            logger.warning(
                "Only a part of the pretrained weights was loaded. "
                "Some layers can't be initialized with pretrained "
                f"weights: {e}"
            )

        for layer_name in re.findall("size mismatch for (.*?):", str(e)):
            model_weights.pop(layer_name, None)
        model.load_state_dict(OrderedDict(model_weights), strict=False)
    return model


def load_checkpoint(
    cfg: Any, model: torch.nn.Module, strict: bool = True, weights_path: str = None
):
    """Load checkpoint

    Args:
        cfg: config file
        model: model to load weights to
        strict: whether to apply strict matching for weights
        weights_path: custom path to the weights.
            If None, cfg.architecture.pretrained_weights is used
    Returns:
        epoch: current epoch
    """

    if weights_path is None:
        weights_path = cfg.architecture.pretrained_weights

    model_weights = torch.load(weights_path, map_location="cpu")["model"]

    model = load_model_weights(model, model_weights, strict, cfg)

    del model_weights
    gc.collect()

    if cfg.environment._local_rank == 0:
        logger.info(f"Weights loaded from: {weights_path}")


def get_ds_config(cfg: Any):
    return {
        "fp16": {
            "enabled": cfg.architecture.backbone_dtype == "float16",
            "loss_scale_window": 100,
        },
        "bf16": {
            "enabled": cfg.architecture.backbone_dtype == "bfloat16",
            "loss_scale_window": 100,
        },
        "zero_force_ds_cpu_optimizer": False,
        "zero_optimization": {
            "stage": 3,
            "overlap_comm": True,
            "contiguous_gradients": True,
            "reduce_bucket_size": cfg.environment.deepspeed_reduce_bucket_size,
            # zero3
            "stage3_prefetch_bucket_size": cfg.environment.deepspeed_stage3_prefetch_bucket_size,  # noqa: E501
            "stage3_param_persistence_threshold": cfg.environment.deepspeed_stage3_param_persistence_threshold,  # noqa: E501
            # zero3 offload cpu
            # "stage3_max_live_parameters": cfg.environment.deepspeed_stage3_max_live_parameters,  # noqa: E501
            # "stage3_max_reuse_distance": cfg.environment.deepspeed_stage3_max_reuse_distance,  # noqa: E501
            # zero++
            # "reduce_scatter": True,
            # "zero_quantized_weights": True,
            # "zero_hpz_partition_size": 16,
            # "zero_quantized_gradients": True,
        },
        "steps_per_print": 2000,
        "train_micro_batch_size_per_gpu": cfg.training.batch_size,
        "gradient_accumulation_steps": cfg.training.grad_accumulation,
        "wall_clock_breakdown": False,
    }


def wrap_model_distributed(
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    lr_scheduler: torch.optim.lr_scheduler._LRScheduler,
    train_dataloader: torch.utils.data.DataLoader,
    val_dataloader: torch.utils.data.DataLoader,
    cfg: Any,
):
    if cfg.environment.use_deepspeed:
        ds_config = get_ds_config(cfg)
        model, optimizer, train_dataloader, lr_scheduler = deepspeed.initialize(
            model=model,
            optimizer=optimizer,
            lr_scheduler=lr_scheduler,
            training_data=train_dataloader.dataset,
            config_params=ds_config,
        )
        val_dataloader = DeepSpeedDataLoader(
            val_dataloader.dataset,
            batch_size=val_dataloader.batch_size,
            local_rank=cfg.environment._local_rank,
            pin_memory=True,
            tput_timer=None,
            data_sampler=OrderedDistributedSampler(
                val_dataloader.dataset,
                num_replicas=cfg.environment._world_size,
                rank=cfg.environment._local_rank,
            ),
        )
    else:
        find_unused_parameters = cfg.environment.find_unused_parameters
        if getattr(cfg.architecture, "gradient_checkpointing", None):
            find_unused_parameters = False
        model = DistributedDataParallel(
            model,
            device_ids=[cfg.environment._local_rank],
            find_unused_parameters=find_unused_parameters,
        )

    return model, optimizer, train_dataloader, val_dataloader, lr_scheduler


def get_optimizer(model: torch.nn.Module, cfg: Any) -> torch.optim.Optimizer:
    """Prepares Optimizer.

    Args:
        model: model
        cfg: input config

    Returns:
        Optimizer
    """
    no_decay = ["bias", "LayerNorm.weight"]
    differential_layers = cfg.training.differential_learning_rate_layers
    optimizer = Optimizers.get(cfg.training.optimizer)(
        [
            {
                "params": [
                    param
                    for name, param in model.named_parameters()
                    if all(layer not in name for layer in differential_layers)
                    and all(nd not in name for nd in no_decay)
                    and param.requires_grad
                ],
                "lr": cfg.training.learning_rate,
                "weight_decay": cfg.training.weight_decay,
            },
            {
                "params": [
                    param
                    for name, param in model.named_parameters()
                    if all(layer not in name for layer in differential_layers)
                    and (any(nd in name for nd in no_decay))
                    and param.requires_grad
                ],
                "lr": cfg.training.learning_rate,
                "weight_decay": 0,
            },
            {
                "params": [
                    param
                    for name, param in model.named_parameters()
                    if (any(layer in name for layer in differential_layers))
                    and all(nd not in name for nd in no_decay)
                    and param.requires_grad
                ],
                "lr": cfg.training.differential_learning_rate,
                "weight_decay": cfg.training.weight_decay,
            },
            {
                "params": [
                    param
                    for name, param in model.named_parameters()
                    if (any(layer in name for layer in differential_layers))
                    and (any(nd in name for nd in no_decay))
                    and param.requires_grad
                ],
                "lr": cfg.training.differential_learning_rate,
                "weight_decay": 0,
            },
        ],
        lr=cfg.training.learning_rate,
        weight_decay=cfg.training.weight_decay,
    )

    return optimizer


def get_scheduler(
    cfg: Any, optimizer: torch.optim.Optimizer, epoch_steps: int
) -> torch.optim.lr_scheduler._LRScheduler:
    """Prepares Learning Rate Scheduler.

    Args:
        cfg: input config
        optimizer: model optimizer
        epoch_steps: total number of weight updates during the epoch

    Returns:
        Learning Rate Scheduler
    """

    return Schedulers.get(cfg.training.schedule)(
        optimizer=optimizer,
        num_warmup_steps=cfg.training.warmup_epochs * epoch_steps,
        num_training_steps=cfg.training.epochs * epoch_steps,
    )


def generate_experiment_name() -> str:
    """
    Generates a random human-readable experiment name in kebab-case.

    Returns:
        The random name.
    """
    return coolname.generate_slug(2)


def reduce_metric(output, reduce=None) -> float:
    """Reduces metric and return metric score (number)

    Args:
        output: output of the model
        reduce: how to reduce the metric over the sample dimension

    Returns:
        score: single number score (using config threshold for threshold metrics)
        or non-reduced array of scores per sample.
    """

    if reduce == "mean":
        score = np.mean(output["metrics"])
    else:
        raise NotImplementedError()

    return score


def get_number_of_validation_epochs(training_epochs: int, evaluation_epochs: float):
    """
    Given the number of training epochs and the number of epochs between model
    evaluations, return the number of times the model is being evaluated during
    training

    Args:
        training_epochs: The number of epochs to train for
        evaluation_epochs: This is the number of epochs after which we want to
            evaluate our model

    Returns:
        num_val_epochs: The number of epochs to be evaluated during training.
    """
    return training_epochs // evaluation_epochs


def contains_nan(output: Dict):
    return (
        sum(
            1
            for key, val in output.items()
            if isinstance(val, torch.Tensor)
            and torch.isnan(val.detach().cpu()).sum() > 0
        )
        > 0
    )


def run_inference(
    cfg: Any,
    model: torch.nn.Module,
    dataloader,
    mode: str,
) -> Dict[str, list]:
    """Runs inference

    Args:
        cfg: config
        model: model
        dataloader: custom dataloader
        mode: mode for inference

    Returns:
        Dictionary with output

    """

    # Store information for evaluation
    out = dict()

    if cfg.environment._local_rank == 0:
        logger.info(f"Starting {mode} inference")

    tqdm_out = TqdmToLogger(logger, level=logging.INFO)
    progress_bar = tqdm(
        total=len(dataloader),
        disable=cfg.environment._local_rank != 0,
        file=tqdm_out,
        ascii=True,
        desc=f"{mode} progress",
        mininterval=0,
    )

    log_update_steps = max(len(dataloader) // 20, 1)
    inf_it = iter(dataloader)
    for itr in range(len(dataloader)):
        try:
            data = next(inf_it)
        except Exception:
            raise LLMDataException("Data reading error. Skipping inference.")

        val_batch_size = get_inference_batch_size(cfg)
        cfg.environment._curr_val_step += val_batch_size * cfg.environment._world_size

        batch = cfg.dataset.dataset_class.batch_to_device(data, cfg.environment._device)

        if cfg.environment.use_deepspeed:
            if (
                cfg.prediction.metric != "Perplexity"
                and cfg.problem_type != "text_causal_classification_modeling"
            ):
                output = {
                    "predicted_answer_ids": model.module.generate(batch, cfg)
                    .detach()
                    .cpu()
                }
            else:
                output = model.forward(batch)
        else:
            with autocast(enabled=cfg.environment.mixed_precision):
                if (
                    cfg.prediction.metric != "Perplexity"
                    and cfg.problem_type != "text_causal_classification_modeling"
                ):
                    output = {
                        "predicted_answer_ids": unwrap_model(model)
                        .generate(batch, cfg)
                        .detach()
                        .cpu()
                    }
                else:
                    output = model.forward(batch)
        if contains_nan(output) and cfg.environment.mixed_precision:
            raise LLMModelException(
                "NaN caught during mixed precision inference. "
                "Please disable mixed precision inference. "
                "Alternatively, reducing learning rate or "
                "gradient clipping may help to stabilize training."
            )

        output = dataloader.dataset.postprocess_batch_predictions(output=output)

        if "predicted_answer_ids" in output.keys():
            del output["predicted_answer_ids"]

        for key, val in output.items():
            if isinstance(val, torch.Tensor):
                val = val.detach().cpu()

            # DefaultDict is not used as it adds extra keys during pickle.dump
            if key not in out:
                out[key] = [val]
            else:
                out[key] += [val]

        if cfg.environment._local_rank == 0:
            # Show logs each 5% of the inference
            if (itr + 1) % log_update_steps == 0 or itr == len(dataloader) - 1:
                progress_bar.set_description(f"{mode} progress", refresh=False)
                if (itr + 1) % log_update_steps == 0:
                    progress_bar.update(log_update_steps)
                else:
                    progress_bar.update(len(dataloader) % log_update_steps)

            cfg.logging._logger.log(
                "internal",
                "current_val_step",
                cfg.environment._curr_val_step,
                step=cfg.environment._curr_val_step,
            )

        if cfg.environment._distributed:
            torch.distributed.barrier()

    progress_bar.close()
    del progress_bar
    out = cat_batches(out)

    return out


def save_predictions(cfg, val_data, val_dataloader, val_df, mode):
    val_data, val_df = val_dataloader.dataset.format_output(  # type: ignore
        cfg=cfg, df=val_df, output=val_data
    )
    raw_preds_name = os.path.join(cfg.output_directory, f"{mode}_raw_predictions.pkl")
    csv_preds_name = os.path.join(cfg.output_directory, f"{mode}_predictions.csv")
    save_pickle(raw_preds_name, val_data)
    val_df.to_csv(csv_preds_name, index=False)


def update_backbone_config(config: Any, cfg: Any):
    if hasattr(config, "hidden_dropout_prob"):
        config.hidden_dropout_prob = cfg.architecture.intermediate_dropout
    if hasattr(config, "attention_probs_dropout_prob"):
        config.attention_probs_dropout_prob = cfg.architecture.intermediate_dropout
    if (
        not hasattr(config, "hidden_dropout_prob")
        and not hasattr(config, "attention_probs_dropout_prob")
        and cfg.architecture.intermediate_dropout > 0
    ):
        logger.warning(
            "Model config does not have dropout attributes. "
            f"Ignoring Intermediate Dropout = {cfg.architecture.intermediate_dropout}."
        )
        cfg.architecture.intermediate_dropout = 0

    tokenizer = get_tokenizer(cfg)

    if config.eos_token_id != tokenizer.eos_token_id:
        logger.warning(
            "EOS token id not matching between config and tokenizer. "
            "Overwriting with tokenizer id."
        )
        config.eos_token_id = tokenizer.eos_token_id
    if config.pad_token_id != tokenizer.pad_token_id:
        logger.warning(
            "PAD token id not matching between config and tokenizer. "
            "Overwriting with tokenizer id."
        )
        config.pad_token_id = tokenizer.pad_token_id
    # no warning needed as not used
    if config.bos_token_id != tokenizer.bos_token_id:
        config.bos_token_id = tokenizer.bos_token_id

    if "mpt-" in cfg.llm_backbone:
        config.init_device = cfg.environment._device

    # See: https://github.com/huggingface/transformers/pull/24906
    if hasattr(config, "pretraining_tp") and cfg.training.lora:
        logger.info("Setting pretraining_tp of model config to 1.")
        config.pretraining_tp = 1

    return config


def create_nlp_backbone(cfg, model_class=AutoModel) -> Any:
    """
    Creates a backbone model for NLP tasks.
    This is needed for Gradient Checkpointing in DDP mode.
    """
    kwargs = dict()
    try:
        config = AutoConfig.from_pretrained(
            cfg.llm_backbone,
            trust_remote_code=cfg.environment.trust_remote_code,
            use_auth_token=os.getenv("HUGGINGFACE_TOKEN"),
            revision=cfg.environment.huggingface_branch,
        )
        kwargs["use_auth_token"] = os.getenv("HUGGINGFACE_TOKEN")
    except TypeError:
        # TypeError: RWForCausalLM.__init__() got
        # an unexpected keyword argument 'use_auth_token'
        config = AutoConfig.from_pretrained(
            cfg.llm_backbone,
            trust_remote_code=cfg.environment.trust_remote_code,
            revision=cfg.environment.huggingface_branch,
        )

    config = update_backbone_config(config, cfg)

    quantization_config = None
    if cfg.architecture.backbone_dtype == "int8":
        kwargs["device_map"] = {"": cfg.environment._device}  # type: ignore
        quantization_config = BitsAndBytesConfig(
            load_in_8bit=True,
            llm_int8_threshold=0.0,
        )
        # need to force pretrained
        cfg.architecture.pretrained = True
        kwargs["torch_dtype"] = torch.float16  # type: ignore
    elif cfg.architecture.backbone_dtype == "int4":
        kwargs["device_map"] = {"": cfg.environment._device}  # type: ignore
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_quant_type="nf4",
        )
        # need to force pretrained
        cfg.architecture.pretrained = True
        kwargs["torch_dtype"] = torch.float16  # type: ignore
    else:
        kwargs["torch_dtype"] = getattr(torch, cfg.architecture.backbone_dtype)

    logger.info(f"Using {cfg.architecture.backbone_dtype} for backbone")

    kwargs["trust_remote_code"] = cfg.environment.trust_remote_code

    if cfg.architecture.pretrained:
        backbone = model_class.from_pretrained(
            cfg.llm_backbone,
            revision=cfg.environment.huggingface_branch,
            config=config,
            quantization_config=quantization_config,
            **kwargs,
        )
    else:
        kwargs.pop("use_auth_token", None)
        backbone = model_class.from_config(config, **kwargs)

    if cfg.tokenizer._vocab_length > config.vocab_size:
        logger.info(f"Resizing token embeddings to {cfg.tokenizer._vocab_length}")
        backbone.resize_token_embeddings(cfg.tokenizer._vocab_length)

    backbone.model_parallel = False

    if cfg.training.lora:
        # if used, gradient checkpointing will be enabled below
        loaded_in_kbit = getattr(backbone, "is_loaded_in_8bit", False) or getattr(
            backbone, "is_loaded_in_4bit", False
        )

        for name, param in backbone.named_parameters():
            # freeze base model's layers
            param.requires_grad = False

        # cast all non INT8 parameters to fp32
        if loaded_in_kbit:
            for param in backbone.parameters():
                if param.dtype in [torch.float16, torch.bfloat16]:
                    param.data = param.data.to(torch.float32)
    elif cfg.architecture.backbone_dtype != "float32":
        if cfg.environment.mixed_precision:
            logger.info("Disabling mixed precision as dtype not set to float32.")
            cfg.environment.mixed_precision = False
        if cfg.architecture.backbone_dtype != "bfloat16":
            logger.warning(
                "Pure float16 or int8 training will "
                "likely lead to unstable training without adapters."
            )

    if cfg.architecture.gradient_checkpointing:
        backbone.gradient_checkpointing_enable()

    if backbone.generation_config.eos_token_id != config.eos_token_id:
        logger.warning(
            "EOS token id not matching between generation config and tokenizer. "
            "Overwriting with tokenizer id."
        )
        backbone.generation_config.eos_token_id = config.eos_token_id
    if backbone.generation_config.pad_token_id != config.pad_token_id:
        logger.warning(
            "PAD token id not matching between generation config and tokenizer. "
            "Overwriting with tokenizer id."
        )
        backbone.generation_config.pad_token_id = config.pad_token_id
    # no warning needed as not used
    if backbone.generation_config.bos_token_id != config.bos_token_id:
        backbone.generation_config.bos_token_id = config.bos_token_id

    return backbone, config


class TokenStoppingCriteria(StoppingCriteria):
    """
    Stopping criteria based on tokens.
    Will stop generation when each generated sample contains at least one of the
    stop_word_ids.
    """

    def __init__(self, stop_word_ids, prompt_input_ids_len):
        super().__init__()
        self.prompt_input_ids_len = prompt_input_ids_len
        if stop_word_ids is None:
            stop_word_ids = []
        self.stop_word_ids = stop_word_ids

    def should_stop(
        self,
        generated_ids: torch.Tensor,
        stop_word_id: torch.Tensor,
    ):
        if len(stop_word_id.shape) == 0:
            return (
                torch.mean(((generated_ids == stop_word_id).sum(1) > 0).float()) == 1
            ).item()
        else:
            return (
                self.get_num_vector_found_in_matrix_rows(stop_word_id, generated_ids)
                == generated_ids.shape[0]
            )

    @staticmethod
    def get_num_vector_found_in_matrix_rows(vector, matrix):
        """
        Count the number of times a vector is found in a matrix row.
        If the vector is found in a row, the search stops and the next row is searched.
        """
        assert len(vector.shape) == 1
        assert len(matrix.shape) == 2

        found = 0
        for row in matrix:
            # stride through the vector
            for i in range(len(row) - len(vector) + 1):
                # check if the vector contains the tensor
                if torch.all(row[i : i + len(vector)] == vector):
                    found += 1
                    break

        return found

    def __call__(self, input_ids: torch.Tensor, scores: torch.FloatTensor, **kwargs):
        generated_ids: torch.Tensor = input_ids[:, self.prompt_input_ids_len :]
        for stop_word_id in self.stop_word_ids:
            if self.should_stop(generated_ids, stop_word_id.to(generated_ids.device)):
                if generated_ids.shape[1] == 1:
                    logger.warning(
                        f"Stopping criteria triggered for {stop_word_id} at first "
                        "generated token."
                    )
                return True
        return False


def prepare_lora(cfg, backbone):
    target_modules = (
        [
            lora_target_module.strip()
            for lora_target_module in cfg.training.lora_target_modules.strip().split(  # noqa: E501
                ","
            )
        ]
        if cfg.training.lora_target_modules
        else None
    )

    if target_modules is None:
        target_modules = []
        for name, module in backbone.named_modules():
            if (
                isinstance(module, (torch.nn.Linear, torch.nn.Conv1d))
                and "head" not in name
            ):
                name = name.split(".")[-1]
                if name not in target_modules:
                    target_modules.append(name)

    if cfg.environment._local_rank == 0:
        logger.info(f"Lora module names: {target_modules}")

    lora_config = LoraConfig(
        r=cfg.training.lora_r,
        lora_alpha=cfg.training.lora_alpha,
        target_modules=target_modules,
        lora_dropout=cfg.training.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
    )
    if cfg.architecture.gradient_checkpointing:
        backbone.enable_input_require_grads()
    backbone = get_peft_model(backbone, lora_config)
    backbone.print_trainable_parameters()
    return backbone


def generate(backbone, batch, cfg, streamer, remove_prompt=True):
    mask_key = "prompt_attention_mask"
    pad_keys = [
        "prompt_input_ids",
        "prompt_attention_mask",
    ]
    batch = batch_padding(
        cfg,
        batch,
        training=False,
        mask_key=mask_key,
        pad_keys=pad_keys,
    )
    input_ids = batch["prompt_input_ids"]
    attention_mask = batch["prompt_attention_mask"]
    # Adding GenerationMixin type annotation for faster lookup
    generation_function: GenerationMixin.generate = backbone.generate
    verbosity = transformers_logging.get_verbosity()
    stopping_criteria = StoppingCriteriaList(
        [
            TokenStoppingCriteria(
                stop_word_ids=cfg.tokenizer._stop_words_ids,
                prompt_input_ids_len=input_ids.shape[1],
            )
        ]
    )
    # force to use cache and disable gradient checkpointing if enabled
    backbone.config.use_cache = True
    if cfg.architecture.gradient_checkpointing:
        backbone.gradient_checkpointing_disable()
    transformers_logging.set_verbosity_error()
    output = generation_function(
        inputs=input_ids,
        attention_mask=attention_mask,
        generation_config=backbone.generation_config,
        min_new_tokens=cfg.prediction.min_length_inference,
        max_new_tokens=cfg.prediction.max_length_inference,
        do_sample=cfg.prediction.do_sample,
        num_beams=cfg.prediction.num_beams,
        temperature=(float(cfg.prediction.temperature)),
        repetition_penalty=(float(cfg.prediction.repetition_penalty)),
        top_k=cfg.prediction.top_k,
        top_p=float(cfg.prediction.top_p),
        stopping_criteria=stopping_criteria,
        renormalize_logits=True,
        return_dict_in_generate=False,
        use_cache=True,
        streamer=streamer,
    )
    transformers_logging.set_verbosity(verbosity)
    # enable checkpointing again
    if cfg.architecture.gradient_checkpointing:
        backbone.gradient_checkpointing_enable()
    if remove_prompt:
        output = output[:, input_ids.shape[1] :]
    return output
