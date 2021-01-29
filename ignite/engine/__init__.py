from collections.abc import Mapping
from distutils.version import LooseVersion
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

import torch

import ignite.distributed as idist
from ignite.engine.deterministic import DeterministicEngine
from ignite.engine.engine import Engine
from ignite.engine.events import CallableEventWithFilter, EventEnum, Events, EventsList, RemovableEventHandle, State
from ignite.metrics import Metric
from ignite.utils import convert_tensor

if idist.has_xla_support:
    import torch_xla.core.xla_model as xm


__all__ = [
    "State",
    "create_supervised_trainer",
    "create_supervised_evaluator",
    "Engine",
    "DeterministicEngine",
    "Events",
    "EventsList",
    "EventEnum",
    "CallableEventWithFilter",
    "RemovableEventHandle",
]


def _prepare_batch(
    batch: Sequence[torch.Tensor], device: Optional[Union[str, torch.device]] = None, non_blocking: bool = False
) -> Tuple[Union[torch.Tensor, Sequence, Mapping, str, bytes], ...]:
    """Prepare batch for training: pass to a device with options.

    """
    x, y = batch
    return (
        convert_tensor(x, device=device, non_blocking=non_blocking),
        convert_tensor(y, device=device, non_blocking=non_blocking),
    )


def create_supervised_trainer(
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    loss_fn: Union[Callable, torch.nn.Module],
    device: Optional[Union[str, torch.device]] = None,
    non_blocking: bool = False,
    prepare_batch: Callable = _prepare_batch,
    output_transform: Callable = lambda x, y, y_pred, loss: loss.item(),
    deterministic: bool = False,
    amp: bool = False,
    scaler: "torch.cuda.amp.GradScaler" = None,
    opt_level: str = "O1",
    **grad_norm_kwargs: Any,
) -> Engine:
    """Factory function for creating a trainer for supervised models.

    Args:
        model (torch.nn.Module): the model to train.
        optimizer (torch.optim.Optimizer): the optimizer to use.
        loss_fn (torch.nn loss function): the loss function to use.
        device (str, optional): device type specification (default: None).
            Applies to batches after starting the engine. Model *will not* be moved.
            Device can be CPU, GPU or TPU.
        non_blocking (bool, optional): if True and this copy is between CPU and GPU, the copy may occur asynchronously
            with respect to the host. For other cases, this argument has no effect.
        prepare_batch (callable, optional): function that receives `batch`, `device`, `non_blocking` and outputs
            tuple of tensors `(batch_x, batch_y)`.
        output_transform (callable, optional): function that receives 'x', 'y', 'y_pred', 'loss' and returns value
            to be assigned to engine's state.output after each iteration. Default is returning `loss.item()`.
        deterministic (bool, optional): if True, returns deterministic engine of type
            :class:`~ignite.engine.deterministic.DeterministicEngine`, otherwise :class:`~ignite.engine.engine.Engine`
            (default: False).
        amp (bool, optional): if True, model and optimizer will be casted to float16 using ``torch.cuda.amp``
            if ``torch>=1.6.0`` else using ``apex``. (default: False)
        scaler (GradScaler, optional): if ``amp`` set to True and ``torch>=1.6.0``, this argument must be provided.
        opt_level (str, optional): Pure or mixed precision optimization level. Accepted values are
            “O0”, “O1”, “O2”, and “O3”.
        grad_norm_kwargs (Any, optional): kwargs passed to ``torch.nn.utils.clip_grad_norm_``.

    Note:
        `engine.state.output` for this engine is defined by `output_transform` parameter and is the loss
        of the processed batch by default.

    .. warning::

        The internal use of `device` has changed.
        `device` will now *only* be used to move the input data to the correct device.
        The `model` should be moved by the user before creating an optimizer.
        For more information see:

        - `PyTorch Documentation <https://pytorch.org/docs/stable/optim.html#constructing-it>`_

        - `PyTorch's Explanation <https://github.com/pytorch/pytorch/issues/7844#issuecomment-503713840>`_

    Returns:
        Engine: a trainer engine with supervised update function.
    """

    device_type = device.type if isinstance(device, torch.device) else device
    on_tpu = "xla" in device_type if device_type is not None else False

    if on_tpu and not idist.has_xla_support:
        raise RuntimeError("In order to run on TPU, please install PyTorch XLA")

    if amp and LooseVersion(torch.__version__) >= LooseVersion("1.6.0"):
        from torch.cuda.amp import autocast

        is_native_amp = True
    elif amp:
        try:
            from apex import amp

            is_apex_amp = True
        except ImportError:
            raise ImportError("Please install apex from https://github.com/nvidia/apex.")

    def _update(engine: Engine, batch: Sequence[torch.Tensor]) -> Union[Any, Tuple[torch.Tensor]]:
        model.train()
        optimizer.zero_grad()
        x, y = prepare_batch(batch, device=device, non_blocking=non_blocking)
        if is_native_amp and not is_apex_amp:
            with autocast(amp):
                y_pred = model(x)
                loss = loss_fn(y_pred, y)
        else:
            y_pred = model(x)
            loss = loss_fn(y_pred, y)

        if is_apex_amp and not is_native_amp:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        elif not is_apex_amp and is_native_amp:
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)

        if grad_norm_kwargs and not is_apex_amp and is_native_amp:
            torch.nn.utils.clip_grad_norm_(model.parameters(), **grad_norm_kwargs)
        elif grad_norm_kwargs and is_apex_amp and not is_native_amp:
            torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), **grad_norm_kwargs)

        if on_tpu and not amp:
            xm.optimizer_step(optimizer, barrier=True)
        elif is_native_amp and not is_apex_amp:
            scaler.step(optimizer)
            scaler.update()
        elif not is_native_amp and is_apex_amp:
            optimizer.step()

        return output_transform(x, y, y_pred, loss)

    trainer = Engine(_update) if not deterministic else DeterministicEngine(_update)

    return trainer


def create_supervised_evaluator(
    model: torch.nn.Module,
    metrics: Optional[Dict[str, Metric]] = None,
    device: Optional[Union[str, torch.device]] = None,
    non_blocking: bool = False,
    prepare_batch: Callable = _prepare_batch,
    output_transform: Callable = lambda x, y, y_pred: (y_pred, y),
) -> Engine:
    """
    Factory function for creating an evaluator for supervised models.

    Args:
        model (`torch.nn.Module`): the model to train.
        metrics (dict of str - :class:`~ignite.metrics.Metric`): a map of metric names to Metrics.
        device (str, optional): device type specification (default: None).
            Applies to batches after starting the engine. Model *will not* be moved.
        non_blocking (bool, optional): if True and this copy is between CPU and GPU, the copy may occur asynchronously
            with respect to the host. For other cases, this argument has no effect.
        prepare_batch (callable, optional): function that receives `batch`, `device`, `non_blocking` and outputs
            tuple of tensors `(batch_x, batch_y)`.
        output_transform (callable, optional): function that receives 'x', 'y', 'y_pred' and returns value
            to be assigned to engine's state.output after each iteration. Default is returning `(y_pred, y,)` which fits
            output expected by metrics. If you change it you should use `output_transform` in metrics.

    Note:
        `engine.state.output` for this engine is defind by `output_transform` parameter and is
        a tuple of `(batch_pred, batch_y)` by default.

    .. warning::

        The internal use of `device` has changed.
        `device` will now *only* be used to move the input data to the correct device.
        The `model` should be moved by the user before creating an optimizer.

        For more information see:

        - `PyTorch Documentation <https://pytorch.org/docs/stable/optim.html#constructing-it>`_

        - `PyTorch's Explanation <https://github.com/pytorch/pytorch/issues/7844#issuecomment-503713840>`_

    Returns:
        Engine: an evaluator engine with supervised inference function.
    """
    metrics = metrics or {}

    def _inference(engine: Engine, batch: Sequence[torch.Tensor]) -> Union[Any, Tuple[torch.Tensor]]:
        model.eval()
        with torch.no_grad():
            x, y = prepare_batch(batch, device=device, non_blocking=non_blocking)
            y_pred = model(x)
            return output_transform(x, y, y_pred)

    evaluator = Engine(_inference)

    for name, metric in metrics.items():
        metric.attach(evaluator, name)

    return evaluator
