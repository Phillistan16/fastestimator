# Copyright 2019 The FastEstimator Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
from typing import Any, Dict, Iterable, List, Optional, Set, TypeVar, Union

import tensorflow as tf
import torch

from fastestimator.backend.update_model import update_model
from fastestimator.op.tensorop.tensorop import TensorOp
from fastestimator.util.traceability_util import traceable

Tensor = TypeVar('Tensor', tf.Tensor, torch.Tensor)
Model = TypeVar('Model', tf.keras.Model, torch.nn.Module)


@traceable()
class UpdateOp(TensorOp):
    """This class performs updates to a model's weights based on the loss.

    Args:
        model: Model instance compiled by fe.build.
        loss_name: The name of loss.
        mode: What mode(s) to execute this Op in. For example, "train", "eval", "test", or "infer". To execute
            regardless of mode, pass None. To execute in all modes except for a particular one, you can pass an argument
            like "!infer" or "!train".
    """
    def __init__(self,
                 model: Union[tf.keras.Model, torch.nn.Module],
                 loss_name: str,
                 mode: Union[None, str, Iterable[str]] = "train"):
        super().__init__(inputs=loss_name, outputs=None, mode=mode)
        self.model = model
        self.retain_graph = False
        self.weight_decay = isinstance(self.model, tf.keras.Model) and self.model.losses
        if not hasattr(self.model, "loss_name"):
            self.model.loss_name = {loss_name}
        else:
            self.model.loss_name.add(loss_name)

    def get_fe_models(self) -> Set[Model]:
        return {self.model}

    def get_fe_loss_keys(self) -> Set[str]:
        return set(self.inputs)

    def fe_retain_graph(self, retain: Optional[bool] = None) -> Optional[bool]:
        if retain is not None:
            self.retain_graph = retain
        return self.retain_graph

    def forward(self, data: Union[Tensor, List[Tensor]], state: Dict[str, Any]) -> None:
        if not state["warmup"]:
            if self.weight_decay:
                data = data + tf.reduce_sum(self.model.losses)
            update_model(self.model, data, tape=state['tape'], retain_graph=self.retain_graph)
