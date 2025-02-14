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
import multiprocessing as mp
import os
import random
import time
from copy import deepcopy
from typing import Any, Callable, Dict, List, MutableMapping, Optional, Set, TypeVar, Union

import numpy as np
import tensorflow as tf
from torch.utils.data import DataLoader, Dataset, RandomSampler
from torch.utils.data.dataloader import default_collate

from fastestimator.dataset.batch_dataset import BatchDataset
from fastestimator.dataset.op_dataset import OpDataset
from fastestimator.op.numpyop.meta.one_of import OneOf
from fastestimator.op.numpyop.meta.sometimes import Sometimes
from fastestimator.op.numpyop.numpyop import NumpyOp, forward_numpyop
from fastestimator.schedule.schedule import Scheduler, get_current_items
from fastestimator.util.traceability_util import traceable
from fastestimator.util.util import pad_batch, to_list, to_set

DataSource = TypeVar('DataSource', Dataset, DataLoader, tf.data.Dataset)


@traceable()
class Pipeline:
    """A data pipeline class that takes care of data pre-processing.

    Args:
        train_data: The training data, or None if no training data is available.
        eval_data: The evaluation data, or None if no evaluation data is available.
        test_data: The testing data, or None if no evaluation data is available.
        batch_size: The batch size to be used by the pipeline. NOTE: This argument is only applicable when using a
            FastEstimator Dataset.
        ops: NumpyOps to be used for pre-processing. NOTE: This argument is only applicable when using a FastEstimator
            Dataset.
        num_process: Number of CPU threads to use for data pre-processing. NOTE: This argument is only applicable when
            using a FastEstimator Dataset. None will default to the system CPU count. Multiprocessing can be disabled by
            passing 0 here, which can be useful for debugging.
        drop_last: Whether to drop the last batch if the last batch is incomplete.
        pad_value: The padding value if batch padding is needed. None indicates that no padding is needed. NOTE: This
            argument is only applicable when using a FastEstimator Dataset.
        collate_fn: Function to merge data into one batch with input being list of elements.
    """
    ops: List[Union[NumpyOp, Scheduler[NumpyOp]]]

    def __init__(self,
                 train_data: Union[None, DataSource, Scheduler[DataSource]] = None,
                 eval_data: Union[None, DataSource, Scheduler[DataSource]] = None,
                 test_data: Union[None, DataSource, Scheduler[DataSource]] = None,
                 batch_size: Union[None, int, Scheduler[Union[int, Dict[str, int]]], Dict[str, int]] = None,
                 ops: Union[None, NumpyOp, Scheduler[NumpyOp], List[Union[NumpyOp, Scheduler[NumpyOp]]]] = None,
                 num_process: Optional[int] = None,
                 drop_last: bool = False,
                 pad_value: Optional[Union[int, float]] = None,
                 collate_fn: Optional[Callable] = None):
        self.data = {x: y for (x, y) in zip(["train", "eval", "test"], [train_data, eval_data, test_data]) if y}
        self.batch_size = batch_size
        self.ops = to_list(ops)
        if mp.get_start_method(allow_none=True) is None and os.name != 'nt':
            mp.set_start_method('fork')
        if mp.get_start_method(allow_none=True) != 'fork':
            print("FastEstimator-Warn: Pipeline multiprocessing is disabled. OS must support the 'fork' start method.")
            num_process = 0
        self.num_process = num_process if num_process is not None else os.cpu_count()
        self.drop_last = drop_last
        self.pad_value = pad_value
        self.collate_fn = collate_fn
        self._verify_inputs(**{k: v for k, v in locals().items() if k != 'self'})

    def _verify_inputs(self, **kwargs) -> None:
        """A helper method to ensure that the Pipeline inputs are valid.

        Args:
            **kwargs: A collection of variable / value pairs to validate.

        Raises:
            AssertionError: If `batch_size`, `ops`, or `num_process` were specified in the absence of a FastEstimator
                Dataset.
        """
        fe_dataset = False
        for dataset in get_current_items(self.data.values()):
            fe_dataset = self._verify_dataset(dataset, **kwargs) or fe_dataset
        if not fe_dataset:
            assert kwargs['batch_size'] is None, "Pipeline only supports batch_size with built-in (FE) datasets"
            assert kwargs['ops'] is None, "Pipeline only supports ops with built-in (FE) datasets"
            assert kwargs['num_process'] is None, "Pipeline only support num_process with built-in (FE) datasets"

    def _verify_dataset(self, dataset: DataSource, **kwargs) -> bool:
        """A helper function to ensure that all of a dataset's arguments are correct.

        Args:
            dataset: The dataset to validate against.
            **kwargs: A selection of variables and their values which must be validated.

        Returns:
            True iff the `dataset` is a PyTorch Dataset (as opposed to a DataLoader or tf.data.Dataset).

        Raises:
            AssertionError: If the `kwargs` are found to be invalid based on the given `dataset`.
            ValueError: If the `dataset` is of an unknown type.
        """
        if isinstance(dataset, Dataset):
            # batch_size check
            for batch_size in get_current_items(to_list(self.batch_size)):
                assert isinstance(batch_size, (int, dict)), "unsupported batch_size format: {}".format(type(batch_size))
                if isinstance(batch_size, dict):
                    assert all([key in {"train", "eval", "test", "infer"} for key in batch_size.keys()]), \
                        "batch size dictionaries must be keyed on mode"
                    assert all([isinstance(val, int) for val in batch_size.values()]), \
                        "batch size dictionary values must be integers"
            # ops check
            for op in get_current_items(self.ops):
                assert isinstance(op, NumpyOp), "unsupported op format, must provide NumpyOp in Pipeline"
            # num_process check
            assert isinstance(self.num_process, int), "number of processes must be an integer"
            return True
        elif isinstance(dataset, (DataLoader, tf.data.Dataset)):
            if kwargs['batch_size'] is not None:
                print("FastEstimator-Warn: batch_size will only be used for built-in dataset")
            if kwargs['ops'] is not None:
                print("FastEstimator-Warn: ops will only be used for built-in dataset")
            if kwargs['num_process'] is not None:
                print("FastEstimator-Warn: num_process will only be used for built-in dataset")
            return False
        else:
            raise ValueError("Unsupported dataset type: {}".format(type(dataset)))

    def get_modes(self, epoch: Optional[int] = None) -> Set[str]:
        """Get the modes for which the Pipeline has data.

        Args:
            epoch: The current epoch index

        Returns:
            The modes for which the Pipeline has data.
        """
        if epoch is None:
            all_modes = set(self.data.keys())
        else:
            all_modes = []
            for mode, dataset in self.data.items():
                if isinstance(dataset, Scheduler):
                    dataset = dataset.get_current_value(epoch)
                if dataset:
                    all_modes.append(mode)
        return to_set(all_modes)

    def benchmark(self,
                  mode: str = "train",
                  epoch: int = 1,
                  num_steps: int = 1000,
                  log_interval: int = 100,
                  detailed: bool = True) -> None:
        """Benchmark the pipeline processing speed.

        Args:
            mode: The execution mode to benchmark. This can be 'train', 'eval' or 'test'.
            epoch: The epoch index to benchmark. Note that epoch indices are 1-indexed.
            num_steps: The maximum number of steps over which to perform the benchmark.
            log_interval: The logging interval.
            detailed: Whether to display the detailed time used by each operator.
        """
        loader = self.get_loader(mode=mode, epoch=epoch)
        if isinstance(loader, tf.data.Dataset):
            loader = loader.take(num_steps)
        start = time.perf_counter()
        for idx, _ in enumerate(loader, start=1):
            if idx % log_interval == 0:
                duration = time.perf_counter() - start
                iters_per_sec = log_interval / duration
                print("FastEstimator: Step: {}, Epoch: {}, Steps/sec: {}".format(idx, epoch, iters_per_sec))
                start = time.perf_counter()
            if idx == num_steps:
                break
        # Pipeline Operations Benchmarking when using FEDataset
        if isinstance(loader, DataLoader) and isinstance(loader.dataset, OpDataset) and detailed:
            op_list = loader.dataset.ops
            duration_list = np.zeros(shape=(len(op_list)))

            data_len = len(loader.dataset.dataset)
            if self.batch_size:
                batch_size = self.batch_size.get_current_value(epoch) if isinstance(self.batch_size,
                                                                                    Scheduler) else self.batch_size
                batch_size = batch_size[mode] if isinstance(batch_size, dict) else batch_size
                log_interval = log_interval * batch_size

            print("\nBreakdown of time taken by Pipeline Operations ({} epoch {})".format(mode, epoch))
            for _ in range(log_interval):
                index = np.random.randint(data_len)
                items = deepcopy(loader.dataset.dataset[index])
                if isinstance(loader.dataset.dataset, BatchDataset):
                    # BatchDataset may randomly sample the same elements multiple times, so need to avoid reprocessing
                    unique_samples = set()
                    for item in items:
                        if id(item) not in unique_samples:
                            for i, op in enumerate(op_list):
                                start = time.perf_counter()
                                forward_numpyop([op], item, {'mode': loader.dataset.mode})
                                duration = time.perf_counter() - start
                                duration_list[i] += duration
                            unique_samples.add(id(item))
                else:
                    for i, op in enumerate(op_list):
                        start = time.perf_counter()
                        forward_numpyop([op], items, {'mode': loader.dataset.mode})
                        duration = time.perf_counter() - start
                        duration_list[i] += duration

            total_time = np.sum(duration_list)
            op_names = ["Op"]

            for op in op_list:
                if isinstance(op, Sometimes) and op.op:
                    op_names.append(op.__class__.__name__ + " (" + op.op.__class__.__name__ + ")")
                elif isinstance(op, OneOf) and op.ops:
                    op_names.append(op.__class__.__name__ + " (" +
                                    ", ".join([sub_op.__class__.__name__ for sub_op in op.ops]) + ")")
                else:
                    op_names.append(op.__class__.__name__)

            max_op_len = max(len(op_name) for op_name in op_names)
            max_in_len = max([len(", ".join(op.inputs)) for op in op_list] + [len("Inputs")])
            max_out_len = max([len(", ".join(op.outputs)) for op in op_list] + [len("Outputs")])
            print("{}: {}: {}: {}".format("Op".ljust(max_op_len + 1),
                                          "Inputs".ljust(max_in_len + 1),
                                          "Outputs".ljust(max_out_len + 1),
                                          "Time".rjust(5)))
            print("-" * (max_op_len + max_in_len + max_out_len + 15))
            for i, op in enumerate(op_list):
                print("{}: {}: {}: {:5.2f}%".format(op_names[i + 1].ljust(max_op_len + 1),
                                                    ", ".join(op.inputs).ljust(max_in_len + 1),
                                                    ", ".join(op.outputs).ljust(max_out_len + 1),
                                                    100 * duration_list[i] / total_time))

    def get_scheduled_items(self, mode: str) -> List[Any]:
        """Get a list of items considered for scheduling.

        Args:
            mode: Current execution mode.

        Returns:
            List of schedulable items in Pipeline.
        """
        all_items = self.ops + [self.batch_size] + [self.data[mode]]
        return all_items

    def get_epochs_with_data(self, total_epochs: int, mode: str) -> Set[int]:
        """Get a set of epoch indices that contains data given mode.

        Args:
            total_epochs: Total number of epochs.
            mode: Current execution mode.

        Returns:
            Set of epoch indices.
        """
        epochs_with_data = set()
        dataset = self.data[mode]
        if isinstance(dataset, Scheduler):
            epochs_with_data = set(epoch for epoch in range(1, total_epochs + 1) if dataset.get_current_value(epoch))
        elif dataset:
            epochs_with_data = set(range(1, total_epochs + 1))
        return epochs_with_data

    def transform(self, data: Dict[str, Any], mode: str, epoch: int = 1) -> Dict[str, Any]:
        """Apply all pipeline operations on a given data instance for the specified `mode` and `epoch`.

        Args:
            data: Input data in dictionary format.
            mode: The execution mode in which to run. This can be "train", "eval", "test" or "infer".
            epoch: The epoch index to run. Note that epoch indices are 1-indexed.

        Returns:
            The transformed data.
        """
        data = deepcopy(data)
        ops = get_current_items(self.ops, mode, epoch)
        forward_numpyop(ops, data, {'mode': mode})
        for key, value in data.items():
            data[key] = np.expand_dims(value, 0)
        return data

    def get_results(self, mode: str = "train", epoch: int = 1, num_steps: int = 1,
                    shuffle: bool = False) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
        """Get sample Pipeline outputs.

        Args:
            mode: The execution mode in which to run. This can be "train", "eval", or "test".
            epoch: The epoch index to run. Note that epoch indices are 1-indexed.
            num_steps: Number of steps (batches) to get.
            shuffle: Whether to use shuffling.

        Returns:
            A list of batches of Pipeline outputs.
        """
        results = []
        loader = self.get_loader(mode=mode, epoch=epoch, shuffle=shuffle)
        if isinstance(loader, tf.data.Dataset):
            loader = loader.take(num_steps)
        for idx, batch in enumerate(loader, start=1):
            results.append(batch)
            if idx == num_steps:
                break
        if len(results) == 1:
            results = results[0]
        return results

    def get_loader(self,
                   mode: str,
                   epoch: int = 1,
                   shuffle: Optional[bool] = None,
                   output_keys: Optional[Set[str]] = None) -> Union[DataLoader, tf.data.Dataset]:
        """Get a data loader from the Pipeline for a given `mode` and `epoch`.

        Args:
            mode: The execution mode for the loader. This can be 'train', 'eval' or 'test'.
            epoch: The epoch index for the loader. Note that epoch indices are 1-indexed.
            shuffle: Whether to shuffle the data. If None, the value for shuffle is based on mode. NOTE: This argument
                is only used with FastEstimator Datasets.
            output_keys: What keys can be produced from pipeline. If None, all keys will be considered.

        Returns:
            A data loader for the given `mode` and `epoch`.
        """
        data = self.data[mode]
        if isinstance(data, Scheduler):
            data = data.get_current_value(epoch)
        if isinstance(data, Dataset):
            # batch size
            batch_size = self.batch_size
            if isinstance(batch_size, Scheduler):
                batch_size = batch_size.get_current_value(epoch)
            if isinstance(batch_size, dict):
                batch_size = batch_size[mode]
            # batch dataset
            if isinstance(data, BatchDataset):
                data.pad_value = self.pad_value
            # shuffle
            if shuffle is None:
                shuffle = mode == "train" and batch_size is not None
            # collate_fn
            collate_fn = self.collate_fn
            if collate_fn is None and self.pad_value is not None:
                collate_fn = self._pad_batch_collate
            op_dataset = OpDataset(data,
                                   get_current_items(self.ops, mode, epoch),
                                   mode,
                                   output_keys,
                                   deep_remainder=False)
            # Results will be immediately converted to tensors, so don't need deep_remainder
            batch_size = None if isinstance(data, BatchDataset) else batch_size
            data = DataLoader(op_dataset,
                              batch_size=batch_size,
                              shuffle=False if isinstance(data, BatchDataset) else shuffle,
                              sampler=RandomSampler(op_dataset) if isinstance(data, BatchDataset) and shuffle else None,
                              num_workers=self.num_process,
                              drop_last=False if batch_size is None else self.drop_last,
                              worker_init_fn=lambda _: np.random.seed(random.randint(0, 2**32 - 1)),
                              collate_fn=collate_fn)
        return data

    def _pad_batch_collate(self, batch: List[MutableMapping[str, Any]]) -> Dict[str, Any]:
        """A collate function which pads a batch of data.

        Args:
            batch: The data to be batched and collated.

        Returns:
            A padded and collated batch of data.
        """
        pad_batch(batch, self.pad_value)
        return default_collate(batch)
