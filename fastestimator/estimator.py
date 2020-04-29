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
from collections import ChainMap, deque
from typing import Any, Dict, Iterable, List, Optional, Set, Union

import tensorflow as tf
from tensorflow.python.distribute.input_lib import DistributedDataset
from torch.utils.data import DataLoader

from fastestimator.backend.to_shape import to_shape
from fastestimator.backend.to_tensor import to_tensor
from fastestimator.backend.to_type import to_type
from fastestimator.dataset.batch_dataset import BatchDataset
from fastestimator.network import BaseNetwork, TFNetwork, TorchNetwork
from fastestimator.pipeline import Pipeline
from fastestimator.schedule.schedule import Scheduler, get_current_items, get_signature_epochs
from fastestimator.summary.system import Summary, System
from fastestimator.trace.io.best_model_saver import BestModelSaver
from fastestimator.trace.io.model_saver import ModelSaver
from fastestimator.trace.trace import EvalEssential, Logger, Trace, TrainEssential
from fastestimator.util.data import Data
from fastestimator.util.util import Suppressor, draw, to_list, to_set


class Estimator:
    """One class to rule them all.

    Estimator is the highest level class within FastEstimator. It is the class which is invoked to actually train
    (estimator.fit) or test (estimator.test) models. It wraps `Pipeline`, `Network`, `Trace` objects together and
    defines the whole optimization process.

    Args:
        pipeline: An fe.Pipeline object that defines the data processing workflow.
        network: An fe.Network object that contains models and other training graph definitions.
        epochs: The number of epochs to run.
        max_steps_per_epoch: Maximum steps to run for each epoch. If None, all data will be used.
        traces: What Traces to run during training. If None, only the system's default Traces will be included.
        log_steps: Frequency (in steps) for printing log messages. 0 to disable all step-based printing (though epoch
            information will still print). None to completely disable printing.
        monitor_names: Additional keys from the data dictionary to be written into the logs.
    """
    pipeline: Pipeline
    traces: List[Trace]
    monitor_names: Set[str]

    def __init__(self,
                 pipeline: Pipeline,
                 network: BaseNetwork,
                 epochs: int,
                 max_steps_per_epoch: Optional[int] = None,
                 traces: Union[None, Trace, Scheduler[Trace], Iterable[Union[Trace, Scheduler[Trace]]]] = None,
                 log_steps: Optional[int] = 100,
                 monitor_names: Union[None, str, Iterable[str]] = None):
        self.pipeline = pipeline
        self.network = network
        self.traces = to_list(traces)
        self.traces_in_use = None
        assert log_steps is None or log_steps >= 0, \
            "log_steps must be None or positive (or 0 to disable only train logging)"
        self.monitor_names = to_set(monitor_names) | self.network.get_loss_keys()
        self.system = System(network=network,
                             log_steps=log_steps,
                             total_epochs=epochs,
                             max_steps_per_epoch=max_steps_per_epoch)

    def fit(self, summary: Optional[str] = None) -> Optional[Summary]:
        """Train the network for the number of epochs specified by the estimator's constructor.

        Args:
            summary: A name for the experiment. If provided, the log history will be recorded in-memory and returned as
                a summary object at the end of training.

        Returns:
            A summary object containing the training history for this session iff a `summary` name was provided.
        """
        draw()
        self.system.reset(summary)
        self._prepare_traces(run_modes={"train", "eval"})
        self._warmup()
        self._start(run_modes={"train", "eval"})
        return self.system.summary or None

    def _prepare_traces(self, run_modes: Set[str]) -> None:
        """Prepare information about the traces for training.

        Add default traces into the traces_in_use list, also prints a warning if no model saver trace is detected.

        Args:
            run_modes: The current execution modes.
        """
        self.traces_in_use = [trace for trace in self.traces]
        if self.system.log_steps is not None:
            self.traces_in_use.append(Logger())
        if "train" in run_modes:
            self.traces_in_use.insert(0, TrainEssential(monitor_names=self.monitor_names))
            no_save_warning = True
            for trace in get_current_items(self.traces_in_use, run_modes=run_modes):
                if isinstance(trace, (ModelSaver, BestModelSaver)):
                    no_save_warning = False
            if no_save_warning:
                print("FastEstimator-Warn: No ModelSaver Trace detected. Models will not be saved.")
        if "eval" in run_modes and "eval" in self.pipeline.get_modes():
            self.traces_in_use.insert(1, EvalEssential(monitor_names=self.monitor_names))
        # insert system instance to trace
        for trace in get_current_items(self.traces_in_use, run_modes=run_modes):
            trace.system = self.system

    def test(self, summary: Optional[str] = None) -> Optional[Summary]:
        """Run the pipeline / network in test mode for one epoch.

        Args:
            summary: A name for the experiment. If provided, the log history will be recorded in-memory and returned as
                a summary object at the end of training. If None, the default value will be whatever `summary` name was
                most recently provided to this Estimator's .fit() or .test() methods.

        Returns:
            A summary object containing the training history for this session iff the `summary` name is not None (after
            considering the default behavior above).
        """
        self.system.reset_for_test(summary)
        self._prepare_traces(run_modes={"test"})
        self._start(run_modes={"test"})
        return self.system.summary or None

    @staticmethod
    def _sort_traces(traces: List[Trace], available_outputs: Set[str]) -> List[Trace]:
        """Sort traces to attempt to resolve any dependency issues.

        This is essentially a topological sort, but it doesn't seem worthwhile to convert the data into a graph
        representation in order to get the slightly better asymptotic runtime complexity.

        Args:
            traces: A list of traces (not inside schedulers) to be sorted.
            available_outputs: What output keys are already available for the traces to use.

        Returns:
            The sorted list of `traces`.

        Raises:
            AssertionError: If Traces have circular dependencies or require input keys which are not available.
        """
        sorted_traces = []
        available_outputs = to_set(available_outputs)
        end_traces = deque()
        intermediate_traces = deque()
        intermediate_outputs = set()
        trace_deque = deque(traces)
        while trace_deque:
            trace = trace_deque.popleft()
            ins = set(trace.inputs)
            outs = set(trace.outputs)
            if not ins:
                sorted_traces.append(trace)
                available_outputs |= outs
            elif "*" in ins:
                if outs:
                    end_traces.appendleft(trace)
                else:
                    end_traces.append(trace)
            elif ins <= available_outputs:
                sorted_traces.append(trace)
                available_outputs |= outs
            else:
                intermediate_traces.append(trace)
                intermediate_outputs |= outs

        already_seen = set()
        while intermediate_traces:
            trace = intermediate_traces.popleft()
            ins = set(trace.inputs)
            outs = set(trace.outputs)
            already_seen.add(trace)
            if ins <= available_outputs:
                sorted_traces.append(trace)
                available_outputs |= outs
                already_seen.clear()
            elif ins <= (available_outputs | intermediate_outputs):
                intermediate_traces.append(trace)
            else:
                raise AssertionError("Trace {} has unsatisfiable inputs: {}".format(
                    type(trace).__name__, ", ".join(ins - (available_outputs | intermediate_outputs))))

            if intermediate_traces and len(already_seen) == len(intermediate_traces):
                raise AssertionError("Dependency cycle detected amongst traces: {}".format(", ".join(
                    [type(tr).__name__ for tr in already_seen])))
        sorted_traces.extend(list(end_traces))
        return sorted_traces

    def _warmup(self) -> None:
        """Perform a test run of each pipeline and network signature epoch to make sure that training won't fail later.

        Traces are not executed in the warmup since they are likely to contain state variables which could become
        corrupted by running extra steps.
        """
        all_traces = get_current_items(self.traces_in_use, run_modes={"train", "eval"})
        # sort all_traces
        all_schedules = self.network.ops + [
            model.optimizer for model in self.network.models
        ] + self.pipeline.ops + list(self.pipeline.data.values()) + [self.pipeline.batch_size] + self.traces_in_use
        signature_epochs = get_signature_epochs(all_schedules, self.system.total_epochs)
        monitor_names = self.monitor_names
        for epoch in signature_epochs:
            for mode in self.pipeline.get_modes(epoch) - {"test"}:
                # key checking
                loader = self._configure_loader(self.pipeline.get_loader(mode, epoch))
                with Suppressor():
                    if isinstance(loader, tf.data.Dataset):
                        batch = list(loader.take(1))[0]
                    else:
                        batch = next(iter(loader))
                batch = self._configure_tensor(loader, batch)
                assert isinstance(batch, dict), "please make sure data output format is dictionary"
                pipeline_output_keys = to_set(batch.keys())
                network_output_keys = self.network.get_all_output_keys(mode, epoch)
                trace_input_keys = set()
                trace_output_keys = {"*"}
                traces = get_current_items(self.traces_in_use, run_modes=mode, epoch=epoch)
                for idx, trace in enumerate(traces):
                    if idx > 0:  # ignore TrainEssential and EvalEssential's inputs for unmet requirement checking
                        trace_input_keys.update(trace.inputs)
                    trace_output_keys.update(trace.outputs)
                monitor_names = monitor_names - (pipeline_output_keys | network_output_keys)
                unmet_requirements = trace_input_keys - (pipeline_output_keys | network_output_keys | trace_output_keys)
                assert not unmet_requirements, \
                    "found missing key(s) during epoch {} mode {}: {}".format(epoch, mode, unmet_requirements)
                self._sort_traces(traces, available_outputs=pipeline_output_keys | network_output_keys)
                self.network.load_epoch(mode, epoch, output_keys=trace_input_keys.update(traces[0].inputs), warmup=True)
                self.network.run_step(batch)
                self.network.unload_epoch()
        assert not monitor_names, "found missing key(s): {}".format(monitor_names)

    def _start(self, run_modes: Set[str]) -> None:
        """The outer training loop.

        This method invokes the trace on_begin method, runs the necessary 'train' and 'eval' epochs, and then invokes
        the trace on_end method.

        Args:
            run_modes: The current execution modes.
        """
        all_traces = get_current_items(self.traces_in_use, run_modes=run_modes)
        # sort all_traces
        self._run_traces_on_begin(traces=all_traces)
        if "train" in run_modes or "eval" in run_modes:
            try:
                for self.system.epoch_idx in range(1, self.system.total_epochs + 1):
                    if "train" in self.pipeline.get_modes(epoch=self.system.epoch_idx):
                        self.system.mode = "train"
                        self._run_epoch()
                    if "eval" in self.pipeline.get_modes(epoch=self.system.epoch_idx):
                        self.system.mode = "eval"
                        self._run_epoch()
            except EarlyStop:
                pass  # On early stopping we still want to run the final traces and return results
        else:
            self._run_epoch()
        self._run_traces_on_end(traces=all_traces)

    def _run_epoch(self) -> None:
        """A method to perform an epoch of activity.

        This method requires that the current mode and epoch already be specified within the self.system object.
        """
        traces = get_current_items(self.traces_in_use, run_modes=self.system.mode, epoch=self.system.epoch_idx)
        trace_input_keys = set()
        for trace in traces:
            trace_input_keys.update(trace.inputs)
        loader = self._configure_loader(self.pipeline.get_loader(self.system.mode, self.system.epoch_idx))
        iterator = iter(loader)
        self.network.load_epoch(mode=self.system.mode, epoch=self.system.epoch_idx, output_keys=trace_input_keys)
        self.system.batch_idx = None
        with Suppressor():
            batch = next(iterator)
        traces = self._sort_traces(traces, available_outputs=to_set(batch.keys()) |
                                   self.network.get_all_output_keys(self.system.mode, self.system.epoch_idx))
        self._run_traces_on_epoch_begin(traces=traces)
        while True:
            try:
                if self.system.mode == "train":
                    self.system.update_global_step()
                self.system.update_batch_idx()
                self._run_traces_on_batch_begin(traces=traces)
                batch = self._configure_tensor(loader, batch)
                batch, prediction = self.network.run_step(batch)
                self._run_traces_on_batch_end(batch, prediction, traces=traces)
                if self.system.batch_idx == self.system.max_steps_per_epoch and self.system.mode == "train":
                    break
                with Suppressor():
                    batch = next(iterator)
            except StopIteration:
                break
        self._run_traces_on_epoch_end(traces=traces)
        self.network.unload_epoch()

    def _configure_loader(self, loader: Union[DataLoader, tf.data.Dataset]) -> Union[DataLoader, tf.data.Dataset]:
        """A method to configure a given dataloader for use with this Estimator's Network.

        This method will ensure that the `loader` returns the correct data type (tf.Tensor or torch.Tensor) depending on
         the requirements of the Network. It also handles issues with multi-gpu data sharding.

        Args:
            loader: A data loader to be modified.

        Returns:
            The potentially modified dataloader to be used for training.
        """
        new_loader = loader
        if isinstance(new_loader, DataLoader) and isinstance(self.network, TFNetwork):
            add_batch = True
            if hasattr(loader.dataset, "dataset") and isinstance(loader.dataset.dataset, BatchDataset):
                add_batch = False
            batch = to_tensor(loader.dataset[0], target_type="tf")
            data_type = to_type(batch)
            data_shape = to_shape(batch, add_batch=add_batch, exact_shape=False)
            new_loader = tf.data.Dataset.from_generator(lambda: loader, data_type, output_shapes=data_shape)
            new_loader = new_loader.prefetch(1)
        if isinstance(new_loader, tf.data.Dataset):
            if self.system.max_steps_per_epoch and self.system.mode == "train":
                new_loader = new_loader.take(self.system.max_steps_per_epoch)
            if isinstance(tf.distribute.get_strategy(),
                          tf.distribute.MirroredStrategy) and not isinstance(new_loader, DistributedDataset):
                new_loader = tf.distribute.get_strategy().experimental_distribute_dataset(new_loader)
        return new_loader

    def _configure_tensor(self, loader: Union[DataLoader, tf.data.Dataset], batch: Dict[str, Any]) -> Dict[str, Any]:
        """A function to convert a batch of tf.Tensors to torch.Tensors if required.

        Returns:
            Either the original `batch`, or the `batch` converted to torch.Tensors if required.
        """
        if isinstance(loader, tf.data.Dataset) and isinstance(self.network, TorchNetwork):
            batch = to_tensor(batch, target_type="torch")
        return batch

    def _run_traces_on_begin(self, traces: Iterable[Trace]) -> None:
        """Invoke the on_begin methods of given traces.

        Args:
            traces: List of traces.
        """
        data = Data()
        for trace in traces:
            trace.on_begin(data)
        self._check_early_exit()

    def _run_traces_on_epoch_begin(self, traces: Iterable[Trace]) -> None:
        """Invoke the on_epoch_begin methods of given traces.

        Args:
            traces: List of traces.
        """
        data = Data()
        for trace in traces:
            trace.on_epoch_begin(data)
        self._check_early_exit()

    def _run_traces_on_batch_begin(self, traces: Iterable[Trace]) -> None:
        """Invoke the on_batch_begin methods of given traces.

        Args:
            traces: List of traces.
        """
        data = Data()
        for trace in traces:
            trace.on_batch_begin(data)
        self._check_early_exit()

    def _run_traces_on_batch_end(self, batch: Dict[str, Any], prediction: Dict[str, Any],
                                 traces: Iterable[Trace]) -> None:
        """Invoke the on_batch_end methods of given traces.

        Args:
            batch: The batch data which was provided by the pipeline.
            prediction: The prediction data which was generated by the network.
            traces: List of traces.
        """
        data = Data(ChainMap(prediction, batch))
        for trace in traces:
            trace.on_batch_end(data)
        self._check_early_exit()

    def _run_traces_on_epoch_end(self, traces: Iterable[Trace]) -> None:
        """Invoke the on_epoch_end methods of of given traces.

        Args:
            traces: List of traces.
        """
        data = Data()
        for trace in traces:
            trace.on_epoch_end(data)
        self._check_early_exit()

    @staticmethod
    def _run_traces_on_end(traces: Iterable[Trace]) -> None:
        """Invoke the on_end methods of given traces.

        Args:
            traces: List of traces.
        """
        data = Data()
        for trace in traces:
            trace.on_end(data)

    def _check_early_exit(self) -> None:
        """Determine whether training should be prematurely aborted.

        Raises:
            EarlyStop: If the system.stop_training flag has been set to True.
        """
        if self.system.stop_training:
            raise EarlyStop


class EarlyStop(Exception):
    """An exception raised when the system.stop_training flag is flipped by a Trace in order to abort the training.
    """
