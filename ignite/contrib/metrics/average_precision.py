from typing import Callable, Optional, Union

import torch

from ignite.metrics import EpochMetric


class AveragePrecision(EpochMetric):
    """Computes Average Precision accumulating predictions and the ground-truth during an epoch
    and applying `sklearn.metrics.average_precision_score <http://scikit-learn.org/stable/modules/generated/
    sklearn.metrics.average_precision_score.html#sklearn.metrics.average_precision_score>`_ .

    Args:
        output_transform: a callable that is used to transform the
            :class:`~ignite.engine.engine.Engine`'s ``process_function``'s output into the
            form expected by the metric. This can be useful if, for example, you have a multi-output model and
            you want to compute the metric with respect to one of the outputs.
        check_compute_fn: Default False. If True, `average_precision_score
            <http://scikit-learn.org/stable/modules/generated/sklearn.metrics.average_precision_score.html
            #sklearn.metrics.average_precision_score>`_ is run on the first batch of data to ensure there are
            no issues. User will be warned in case there are any issues computing the function.
        device: optional device specification for internal storage.

    AveragePrecision expects y to be comprised of 0's and 1's. y_pred must either be probability estimates or
    confidence values. To apply an activation to y_pred, use output_transform as shown below:

    .. code-block:: python

        def activated_output_transform(output):
            y_pred, y = output
            y_pred = torch.softmax(y_pred, dim=1)
            return y_pred, y

        avg_precision = AveragePrecision(activated_output_transform)

    """

    def __init__(
        self,
        output_transform: Callable = lambda x: x,
        check_compute_fn: bool = False,
        device: Union[str, torch.device] = torch.device("cpu"),
    ):

        try:
            from sklearn.metrics import average_precision_score
        except ImportError:
            raise RuntimeError("This contrib module requires sklearn to be installed.")

        self.average_precision_compute = self.average_precision_compute_fn()

        super(AveragePrecision, self).__init__(
            self.average_precision_compute,
            output_transform=output_transform,
            check_compute_fn=check_compute_fn,
            device=device,
        )

    def average_precision_compute_fn(self) -> Callable[[torch.Tensor, torch.Tensor], float]:
        from sklearn.metrics import average_precision_score

        def wrapper(y_preds: torch.Tensor, y_targets: torch.Tensor) -> float:
            y_true = y_targets.cpu().numpy()
            y_pred = y_preds.cpu().numpy()
            return average_precision_score(y_true, y_pred)

        return wrapper
