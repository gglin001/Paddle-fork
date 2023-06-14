#   Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

import collections
from functools import reduce
from itertools import product

import paddle
from paddle.distributed.utils.nccl_utils import check_nccl_version_for_p2p

from ..utils.log_util import logger

__all__ = ['CommunicateTopology', 'HybridCommunicateGroup']

_HYBRID_PARALLEL_GROUP = None


class ParallelMode:
    """

    There are all the parallel modes currently supported:

        - DATA_PARALLEL: Distribute input data to different devices.
        - TENSOR_PARALLEL: Shards tensors in the network to different devices.
        - PIPELINE_PARALLEL: Place different layers of the network on different devices.
        - SHARDING_PARALLEL: Segment the model parameters, parameter gradients and optimizer states corresponding to the parameters to each device.

    Examples:
        .. code-block:: python

            import paddle
            parallel_mode = paddle.distributed.ParallelMode
            print(parallel_mode.DATA_PARALLEL)  # 0

    """

    DATA_PARALLEL = 0
    TENSOR_PARALLEL = 1
    PIPELINE_PARALLEL = 2
    SHARDING_PARALLEL = 3


class CommunicateTopology:
    def __init__(
        self,
        hybrid_group_names=["data", "pipe", "sharding", "model"],
        dims=[1, 1, 1, 1],
    ):
        self._parallel_names = hybrid_group_names
        self._dims = dims
        self.coordinate = collections.namedtuple(
            'Coordinate', self._parallel_names
        )
        self._world_size = reduce(lambda x, y: x * y, self._dims, 1)

        ranges = [range(d) for d in self._dims]
        all_coordinate = [self.coordinate(*x) for x in product(*ranges)]

        self._coord2rank = dict(zip(all_coordinate, range(len(all_coordinate))))
        self._rank2coord = dict(
            zip(self._coord2rank.values(), self._coord2rank.keys())
        )

    def get_hybrid_group_names(self):
        return self._parallel_names

    def get_dim(self, axis_name):
        return self._dims[self._parallel_names.index(axis_name)]

    def world_size(self):
        return self._world_size

    def get_rank(self, **args):
        assert len(args) == len(self._dims)
        key = self.coordinate(**args)
        assert key in self._coord2rank.keys()
        return self._coord2rank[key]

    def get_coord(self, rank):
        assert rank < self._world_size
        assert rank in self._rank2coord.keys()
        return self._rank2coord[rank]

    def get_axis_list(self, axis_name, index):
        axis = self._parallel_names.index(axis_name)
        ranks = [
            self._coord2rank[coord]
            for coord in self._coord2rank.keys()
            if coord[axis] == index
        ]
        ranks.sort()
        return ranks

    def get_dim_size(self, axis_name):
        assert axis_name in self._parallel_names
        return self._dims[self._parallel_names.index(axis_name)]

    def get_comm_list(self, axis_name):
        assert axis_name in self._parallel_names
        other_axis_names = [
            name for name in self._parallel_names if name != axis_name
        ]

        ranges = []
        for name in other_axis_names:
            dim_num = self.get_dim_size(name)
            ranges.append(range(dim_num))

        all_result = []
        for x in product(*ranges):
            key_coord = {}
            for other_name in other_axis_names:
                key_coord[other_name] = x[other_axis_names.index(other_name)]

            result = []
            for i in range(0, self.get_dim_size(axis_name)):
                key_coord[axis_name] = i
                result.append(self._coord2rank[self.coordinate(**key_coord)])
            all_result.append(result)

        return all_result

    def get_rank_from_stage(self, global_rank, **kwargs):
        coord = self.get_coord(global_rank)
        tf = coord._replace(**kwargs)._asdict()
        return self.get_rank(**tf)


class HybridCommunicateGroup:
    def __init__(self, topology):
        self.nranks = paddle.distributed.get_world_size()
        self.global_rank = paddle.distributed.get_rank()
        self._topo = topology

        self._dp_degree = self._topo.get_dim('data')
        self._mp_degree = self._topo.get_dim('model')
        self._pp_degree = self._topo.get_dim('pipe')
        self._sharding_degree = self._topo.get_dim('sharding')

        self._data_parallel_id = self._get_data_parallel_id()
        self._model_parallel_id = self._get_model_parallel_id()
        self._sharding_parallel_id = self._get_sharding_parallel_id()
        self.stage_id = self._get_pipe_parallel_id()

        assert self._check_vaild_topo(), (
            "Here is an unreasonable topogy setting. world_size: {}, but"
            "mp_num: {}, sharding_num: {}, pp_num: {}, dp_num: {}".format(
                self.nranks,
                self._mp_degree,
                self._sharding_degree,
                self._pp_degree,
                self._dp_degree,
            )
        )

        # create comm group for data parallel
        self._dp_group, self._dp_comm_group = self._set_comm_group("data")

        # create comm group for model parallel
        self._mp_group, self._mp_comm_group = self._set_comm_group("model")

        # create comm group for pipe parallel
        self._pp_group, self._pp_comm_group = self._set_comm_group("pipe")

        # create comm group for sharding parallel
        self._sharding_group, self._sharding_comm_group = self._set_comm_group(
            "sharding"
        )

        # create global group for check inf_nan / clip global norm
        self._check_group, self._check_comm_group = self._set_check_group(
            "data"
        )

        # create p2p group
        self.is_first_stage = self.stage_id == 0
        self.is_last_stage = self.stage_id == (self._pp_degree - 1)

        # create p2p_groups
        if self._pp_degree > 1:
            if paddle.framework.core.is_compiled_with_nccl():
                check_nccl_version_for_p2p()
            self._set_p2p_group()

        debug_str = (
            "HybridParallelInfo: rank_id: %d, mp_degree: %d, "
            "sharding_degree: %d, pp_degree: %d, dp_degree: %d"
            % (
                self.global_rank,
                self._mp_degree,
                self._sharding_degree,
                self._pp_degree,
                self._dp_degree,
            )
        )
        debug_str += ", mp_group: {},  sharding_group: {}, pp_group: {}, dp_group: {}, check/clip group: {}".format(
            self._mp_group,
            self._sharding_group,
            self._pp_group,
            self._dp_group,
            self._check_group,
        )
        logger.info(debug_str)

        global _HYBRID_PARALLEL_GROUP
        _HYBRID_PARALLEL_GROUP = self

    def get_parallel_mode(self):
        # there are four modes : DataParallel / TensorParallel / PipelineParallel / ShardingParallel
        # NOTE when sharding conjugates with other parallel, sharding should act like a optimizer and
        # adding its parallel logic within that parallelism
        # when use sharding alone, it should have its own parallelism for its parallel logic
        # TODO modify 3 others parallel to support sharding
        if (
            self._mp_degree == 1
            and self._pp_degree == 1
            and self._dp_degree == 1
            and self._sharding_degree > 1
        ):
            return ParallelMode.SHARDING_PARALLEL
        elif self._mp_degree == 1 and self._pp_degree == 1:
            return ParallelMode.DATA_PARALLEL
        elif self._mp_degree > 1 and self._pp_degree == 1:
            # initialize the seed
            return ParallelMode.TENSOR_PARALLEL
        elif self._pp_degree > 1:
            return ParallelMode.PIPELINE_PARALLEL

    def _check_vaild_topo(self):
        return (
            self._dp_degree
            * self._mp_degree
            * self._pp_degree
            * self._sharding_degree
            == self.nranks
        )

    def _set_comm_group(self, parallel_method="data"):
        parallel_group = []
        parallel_comm_group = None
        parallel_groups = self._topo.get_comm_list(parallel_method)

        for group in parallel_groups:
            comm_group = paddle.distributed.new_group(ranks=group)
            if self.global_rank in group:
                parallel_group = group
                parallel_comm_group = comm_group

        assert len(parallel_group) > 0
        assert parallel_comm_group is not None

        logger.info(
            "Total {} {} comm group(s) create successfully!".format(
                len(parallel_groups), parallel_method
            )
        )
        return parallel_group, parallel_comm_group

    def _set_check_group(self, parallel_method="data"):
        parallel_group = []
        parallel_comm_group = None
        parallel_size = self._topo.get_dim(parallel_method)
        for idx in range(parallel_size):
            parallel_groups = self._topo.get_axis_list(parallel_method, idx)
            comm_group = paddle.distributed.new_group(ranks=parallel_groups)
            if self.global_rank in parallel_groups:
                parallel_group = parallel_groups
                parallel_comm_group = comm_group

        assert len(parallel_group) > 0
        assert parallel_comm_group is not None

        return parallel_group, parallel_comm_group

    def _get_p2p_next_rank(self):
        assert hasattr(self, 'next_rank'), "next_rank has not been inited"
        return self.next_rank

    def _get_p2p_prev_rank(self):
        assert hasattr(self, 'prev_rank'), "prev_rank has not been inited"
        return self.prev_rank

    def _set_p2p_group(self):
        comm_lists = self._topo.get_comm_list('pipe')

        for comm_ranks in comm_lists:
            assert len(comm_ranks) == self._pp_degree
            for idx, rank in enumerate(comm_ranks):
                curr_rank = rank
                next_rank = comm_ranks[(idx + 1) % self._pp_degree]
                prev_rank = comm_ranks[(idx - 1) % self._pp_degree]

                if self.global_rank == curr_rank:
                    self.next_rank = next_rank
                    self.prev_rank = prev_rank

    def topology(self):
        return self._topo

    def get_global_rank(self):
        return self.global_rank

    # data parallel message:
    def _get_data_parallel_id(self):
        return self._topo.get_coord(self.global_rank).data

    def get_data_parallel_rank(self):
        return self._data_parallel_id

    def get_data_parallel_world_size(self):
        return self._dp_degree

    def get_data_parallel_group(self):
        return self._dp_comm_group

    def get_data_parallel_group_src_rank(self):
        return self._dp_comm_group.ranks[0]

    # model parallel message:
    def _get_model_parallel_id(self):
        return self._topo.get_coord(self.global_rank).model

    def get_model_parallel_rank(self):
        return self._model_parallel_id

    def get_model_parallel_world_size(self):
        return self._mp_degree

    def get_model_parallel_group(self):
        return self._mp_comm_group

    def get_model_parallel_group_src_rank(self):
        return self._mp_comm_group.ranks[0]

    # pipeline parallel message
    def _get_pipe_parallel_id(self):
        return self._topo.get_coord(self.global_rank).pipe

    def get_stage_id(self):
        return self.stage_id

    def get_pipe_parallel_world_size(self):
        return self._pp_degree

    def get_pipe_parallel_group(self):
        return self._pp_comm_group

    def get_p2p_groups(self):
        return None

    # sharding parallel message:
    def _get_sharding_parallel_id(self):
        return self._topo.get_coord(self.global_rank).sharding

    def get_sharding_parallel_rank(self):
        return self._sharding_parallel_id

    def get_sharding_parallel_world_size(self):
        return self._sharding_degree

    def get_sharding_parallel_group(self):
        return self._sharding_comm_group

    def get_sharding_parallel_group_src_rank(self):
        # TODO should the src rank related to the shard rank for each parameter ?
        return self._sharding_comm_group.ranks[0]

    # check parallel group
    def get_check_parallel_group(self):
        return self._check_comm_group

    def get_rank_from_stage(self, stage_id, **kwargs):
        return self._topo.get_rank_from_stage(
            self.global_rank, pipe=stage_id, **kwargs
        )


class _CommunicateGroup:
    """tmp for static"""

    def __init__(self):
        global _HYBRID_PARALLEL_GROUP
        _HYBRID_PARALLEL_GROUP = self
        self.groups = {}

    def set_comm_group(
        self, group_name, group_rank, group_size, ring_id, group_ranks
    ):
        group = paddle.distributed.collective.Group(
            group_rank, ring_id, group_ranks
        )
        self.groups[group_name] = group

    def get_group(self, group_name):
        assert group_name in self.groups
        return self.groups[group_name]

    def get_model_parallel_group(self):
        return self.get_group('model')

    def get_model_parallel_world_size(self):
        return self.get_group('model').nranks

    def get_model_parallel_rank(self):
        return self.get_group('model').rank
